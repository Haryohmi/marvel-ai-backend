from pydantic import BaseModel, Field
from typing import List, Dict
import os
from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnablePassthrough, RunnableParallel
from langchain_core.output_parsers import JsonOutputParser
from langchain_google_genai import GoogleGenerativeAI
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pylatex import Document, Section, Command, NoEscape, Tabular, MultiColumn, Package
from pylatex import Tabular, Tabularx, LongTable
from pylatex.utils import italic, NoEscape, bold

from app.services.logger import setup_logger

logger = setup_logger(__name__)

def read_text_file(file_path):
    # Get the directory containing the script file
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Combine the script directory with the relative file path
    absolute_file_path = os.path.join(script_dir, file_path)
    
    with open(absolute_file_path, 'r') as file:
        return file.read()
    
class RubricGenerator:
    def __init__(self, args=None, vectorstore_class=Chroma, prompt=None, embedding_model=None, model=None, parser=None, verbose=False):
        default_config = {
            "model": GoogleGenerativeAI(model="gemini-1.5-flash"),
            "embedding_model": GoogleGenerativeAIEmbeddings(model='models/embedding-001'),
            "parser": JsonOutputParser(pydantic_object=RubricOutput),
            "prompt": read_text_file("prompt/rubric-generator-prompt.txt"),
            "vectorstore_class": Chroma
        }

        self.prompt = prompt or default_config["prompt"]
        self.model = model or default_config["model"]
        self.parser = parser or default_config["parser"]
        self.embedding_model = embedding_model or default_config["embedding_model"]

        self.vectorstore_class = vectorstore_class or default_config["vectorstore_class"]
        self.vectorstore, self.retriever, self.runner = None, None, None
        self.args = args
        self.verbose = verbose

        if vectorstore_class is None: raise ValueError("Vectorstore must be provided")
        if args.grade_level is None: raise ValueError("Grade Level must be provided")
        if args.point_scale is None: raise ValueError("Point Scale must be provided")
        if int(args.point_scale) < 2 or int(args.point_scale) > 8:
            raise ValueError("Point Scale must be between 2 and 8. Suggested value is 4 for optimal granularity in grading.")
        if args.standard is None: raise ValueError("Learning Standard must be provided")
        if args.lang is None: raise ValueError("Language must be provided")


    def compile(self, documents: List[Document]):
        # Return the chain
        prompt = PromptTemplate(
            template=self.prompt,
            input_variables=["attribute_collection"],
            partial_variables={"format_instructions": self.parser.get_format_instructions()}
        )

        if self.runner is None:
            logger.info(f"Creating vectorstore from {len(documents)} documents") if self.verbose else None
            self.vectorstore = self.vectorstore_class.from_documents(documents, self.embedding_model)
            logger.info(f"Vectorstore created") if self.verbose else None

            self.retriever = self.vectorstore.as_retriever()
            logger.info(f"Retriever created successfully") if self.verbose else None

            self.runner = RunnableParallel(
                {"context": self.retriever,
                "attribute_collection": RunnablePassthrough()
                }
            )

        chain = self.runner | prompt | self.model | self.parser

        logger.info(f"Chain compilation complete")

        return chain
    
    def create_pdf_from_rubric(self, rubric_data):
        # Create a LaTeX document
        doc = Document()

        doc.packages.append(Package('geometry'))
        doc.packages.append(Package('longtable'))
        doc.packages.append(Package('tabularx'))

        doc.preamble.append(NoEscape(r'\geometry{left=1em,right=0.5em}'))

        # Set up the document preamble
        doc.preamble.append(Command('title', 'Rubric'))
        doc.preamble.append(Command('author', 'AI Generated'))
        doc.preamble.append(Command('date', NoEscape(r'\today')))

        doc.append(NoEscape(r'\maketitle'))

        doc.append(NoEscape(r'\noindent\textbf{Title:} ' + rubric_data['title'] + r'\\'))
        doc.append(NoEscape(r'\noindent\textbf{Grade Level:} ' + rubric_data['grade_level'] + r'\\'))   

        # Determine the point scale
        num_points = int(self.args.point_scale)

        first_criteria_description = rubric_data['criterias'][0]['criteria_description']
        points = []

        for i in range(num_points):
            # Append each 'points' from the first_criteria_description to the points list
            points.append(first_criteria_description[i]['points']) 

        # Create the table
        doc.append(NoEscape(r'\section*{Rubric Criterias}'))

        total_columns = num_points + 1
        col_width = f'{0.8/total_columns:.2f}\\textwidth'  # Adjust to fit within margins

        # Create a column definition where each column has equal width
        col_definition = '|' + '|'.join([f'p{{{col_width}}}' for _ in range(total_columns)]) + '|'

        try:
            with doc.create(LongTable(col_definition)) as table:
                # Add table headers
                table.add_hline()
                header_row = ["Criteria"] + points
                table.add_row(header_row)
                table.add_hline()
                table.end_table_header()
                
                # Add footer for continuation
                table.add_hline()
                table.add_row((MultiColumn(num_points + 1, align='r', data='Continued on next page'),))
                table.add_hline()
                table.end_table_footer()
                
                table.end_table_last_footer()

                # Add rows for each criterion
                for criteria in rubric_data['criterias']:
                    row = [criteria['criteria']]  # First column is the 'Criteria' name
                    
                    for criteria_desc in criteria['criteria_description']:
                        # Iterate through the list of descriptions and add each description in a new cell
                        description = ' '.join(criteria_desc['description'])  # Join descriptions with spaces or commas
                        row.append(description)  # Add the concatenated description to the row
                    
                    table.add_row(row)
                    table.add_hline()

        except Exception as e:
            logger.error(f"Error creating table: {str(e)}")

        # Add feedback section
        doc.append(NoEscape(r'\section*{Feedback/Rubric Evaluation}'))
        doc.append(rubric_data['feedback'] + "\n")

        # Generate the PDF
        pdf_filename = 'generated_rubric'
        try:
            doc.generate_pdf(pdf_filename, clean_tex=False)
        except Exception as e:
            logger.error(f"LaTeX Error: {str(e)}")
            with open(f'{pdf_filename}.log', 'r') as log_file:
                logger.error(log_file.read())

        # Construct the full path with .pdf extension
        full_path = f"{os.path.abspath(pdf_filename)}.pdf"

        # Check if the file was created successfully
        if not os.path.exists(full_path):
            logger.error(f"Failed to create PDF file: {full_path}")
        else:
            logger.info(f"Rubric PDF file created successfully: {full_path}")

        return full_path
    
    def validate_rubric(self, response: Dict) -> bool:
         # Check if "criterias" exist and are valid
        if "criterias" not in response or len(response["criterias"]) == 0:
            logger.error("Rubric generation failed, criterias not created successfully, trying agian.")
            return False

        if "feedback" not in response:
            logger.error("Rubric generation failed, feedback not created successfully, trying again.")
            return False

        # Validate each criterion
        criteria_valid = True
        for criterion in response["criterias"]:
            if "criteria_description" not in criterion or len(criterion["criteria_description"]) != int(self.args.point_scale):
                logger.error("Mismatch between point scale nb and a criteria description. Trying again.")
                criteria_valid = False
                break  # Exit the for loop if a criterion is invalid

        if not criteria_valid:
            return False
        
        return True
   
    def create_rubric(self, documents: List[Document]):
        logger.info(f"Creating the Rubric")

        chain = self.compile(documents)

         # Log the input parameters
        input_parameters = (
            f"Grade Level: {self.args.grade_level}, "
            f"Point Scale: {self.args.point_scale}, "
            f"Standard: {self.args.standard}, "
            f"Language (YOU MUST RESPOND IN THIS LANGUAGE): {self.args.lang}"
        )
        logger.info(f"Input parameters: {input_parameters}")

        attempt = 1
        max_attempt = 6

        while attempt < max_attempt:
            try:
                response = chain.invoke(input_parameters)
                logger.info(f"Rubric generated during attempt nb: {attempt}")
            except Exception as e:
                logger.error(f"Error during rubric generation: {str(e)}")
                attempt += 1
                continue
            if response == None:
                logger.error(f"could not generate Rubric, trying again")
                attempt += 1
                continue

            if self.validate_rubric(response) == False:
                attempt += 1
                continue

            # If everything is valid, break the outer loop
            break

        if attempt >= max_attempt:
            raise ValueError("Error: Unable to generate the Rubric after 5 attempts.")
        else:
            logger.info(f"Rubric successfully generated after {attempt} attempt(s).")

        if self.verbose: print(f"Deleting vectorstore")
        self.vectorstore.delete_collection()

        return self.create_pdf_from_rubric(response)
        


class CriteriaDescription(BaseModel):
    points: str = Field(..., description="The total points gained by the student according to the point_scale an the level name")
    description: List[str] = Field(..., description="Description for the specific point on the scale")

class RubricCriteria(BaseModel):
    criteria: str = Field(..., description="name of the criteria in the rubric")
    criteria_description: List[CriteriaDescription] = Field(..., description="Descriptions for each point on the scale")
    
class RubricOutput(BaseModel):
    title: str = Field(..., description="the rubric title of the assignment based on the standard input parameter")
    grade_level: str = Field(..., description="The grade level for which the rubric is created")
    criterias: List[RubricCriteria] = Field(..., description="The grading criteria for the rubric")
    feedback: str = Field(..., description="the feedback provided by the AI model on the generated rubric")
    
