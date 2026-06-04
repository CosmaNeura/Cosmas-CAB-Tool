import os

import openai
import json
import re
import tenacity
import pandas as pd
from step2_ccsr_categorization import ccsr_categories_list
from step3_gpt_doctor_leading_qa import res

if os.environ.get("OPENAI_ORG_ID"):
    openai.organization = os.environ["OPENAI_ORG_ID"]
openai.api_key = os.environ["OPENAI_API_KEY"]

@tenacity.retry(
    stop=tenacity.stop_after_delay(30),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=30),
    retry=tenacity.retry_if_exception_type(openai.error.APIError),
    reraise=True,
)
def gpt_completion(
    prompt,
    engine="gpt-4",
    temp=0,  # set at 0 to ensure consistent completion, increase accuracy along with UUID
    top_p=1.0,
    tokens=500,
    freq_pen=0.25,
    pres_pen=0.0,
    stop=["<<END>>"],
):
    prompt = prompt.encode(encoding="ASCII", errors="ignore").decode()
    response = openai.ChatCompletion.create(
        model=engine,
        messages=[
            {"role": "system", "content":   """You are an AI assistant specialized in biomedical topics. You are provided with a text description from a patient's screening notes. Analyze the patient's notes. Here are your instructions:

                    - Highlight conditions, symptoms, medical history, and any other information that can be mapped to specific CCSR categories.

                    - Keep in mind that the CCSR is used for grouping a large number of diseases into manageable categories for statistical analysis and reporting. 

                    - Ensure the conversation includes information that can guide the mapping to CCSR categories, such as the type of disease, cause, location in the body, and patient's age and sex. 

                    - Highlight medical advice or diagnostic information quoted and summarized from the given information. 

                    - Ensure the output is in markdown bullet point format for clarity.

                    - Encourage the user to consult a healthcare professional for advice."""},

            {"role": "user", "content": prompt},
        ],
        max_tokens=tokens,
        temperature=temp,
        top_p=top_p,
        frequency_penalty=freq_pen,
        presence_penalty=pres_pen,
        stop=stop,
    )
    text = response["choices"][0]["message"]["content"].strip()
    text = re.sub("\s+", " ", text)
    return text

# prompt = """  Ask the patient a medical diagnosis question based on the patient note and sample dialogues dataset. 
#                 Provide supplemental knowledge about the below CCSR categories that can guide the patient to understand the CCSR categories assigned to their patient note. 
#                 The output should be appropriate for the patient's age. 
#                 Ask only one question at a time so the patient will not be distracted or overwhelmed by the medical diligence process.
#                 \n
#                 """

prompt = """Provide an end session screening summary and provide the most recommended medical department for the patient to contact, provide the patient's preliminary medical diagnosis, emphasize on "why" for the diagnosis. 
                
            - Highlight or quote any medication and treatment recommendation based on the patient note and sample dialogues dataset. 
            
            - The output should be appropriate for the patient's age. 
            
            - Ensure the output is in markdown bullet point format for clarity.

            - Encourage the user to consult a healthcare professional for advice.
            \n
            """


input_var_1 = "Patient Notes:"

input_end_session_note = """ 
                        
                        "input": "Hello doctor,I had mumps five months ago and after that, I started to have an infection in my left testes. It was swollen and now it has shrunk to almost half the size of the other one. As I am sexually active, I feel a pain in each of the vas deferens after sex. If I do not have sex for days, they become sensitive. I was treated with Ceftum 500 mg, the first time I had an infection. Now my question is, is there any chance that the infection is still in my body? And, do I need to get examined for it? For the time being, please suggest some precautionary antibiotics for my relief.",

                        \n
                        """

input_var_2 = "Clinical Classifications Software Refined (CCSR) categories listed below: \n"

prompt = prompt + input_var_1 + input_end_session_note + input_var_2

for i, category in enumerate(ccsr_categories_list, start=1):
    content = category['content']
    prompt += f"{i}. {content}\n"

input_dialogues_data = "sample dialogues dataset: \n"

input_var_3 = res 

prompt = prompt + input_dialogues_data + input_var_3


print(gpt_completion(prompt))
