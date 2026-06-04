import os

import openai
import json

openai.api_key = os.environ["OPENAI_API_KEY"]

# Dummy function hard coded to return the same patient notes
def get_patient_notes(patient_id):
    """Get the patient's notes"""
    patient_notes = {
        "patient_id": patient_id,
        "notes": "Hi doctor, I am a 26 year old male. I am 5 feet and 9 inches tall and weigh 255 pounds. When I eat spicy food, I poop blood. Sometimes when I have constipation as well, I poop a little bit of blood. I am really scared that I have colon cancer. I do have diarrhea often. I do not have a family history of colon cancer. I got blood tests done last night. Please find my reports attached.",
    }
    return json.dumps(patient_notes)

def run_conversation():
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo-0613",  # use your actual model here
        messages=[
            {
                "role": "system",
                "content": """You are an AI assistant specialized in biomedical topics. You are provided with a text description from a patient's screening notes. Analyze the patient's notes and provide information useful for Clinical Classifications Software Refined (CCSR) categorization. Here are your instructions:

                - Highlight conditions, symptoms, medical history, and any other information that can be mapped to specific CCSR categories.

                - Keep in mind that the CCSR is used for grouping a large number of diseases into manageable categories for statistical analysis and reporting. 

                - Ensure the conversation includes information that can guide the mapping to CCSR categories, such as the type of disease, cause, location in the body, and patient's age and sex. 

                - Avoid providing medical advice or diagnostic information. 

                - Ensure the output is in markdown bullet point format for clarity.

                - Encourage the user to consult a healthcare professional for advice."""
            },
            {"role": "user", "content": "Can you analyze patient 123's notes and provide useful information for CCSR categorization? Output in markdown bullet points."}
        ],
        functions=[
            {
                "name": "get_patient_notes",
                "description": "Get the patient's notes",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "The id of the patient",
                        },
                    },
                    "required": ["patient_id"],
                },
            }
        ],
        function_call="auto",
    )

    message = response["choices"][0]["message"]

    # Check if the model wants to call a function
    if message.get("function_call"):
        function_name = message["function_call"]["name"]

        # Call the function
        function_response = get_patient_notes(
            patient_id=message.get("patient_id"),
        )

        # Send model the info on the function call and function response
        second_response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo-0613",  # use your actual model here
            messages=[
                {
                    "role": "system",
                    "content": """You are an AI assistant specialized in biomedical topics. You are provided with a text description from a patient's screening notes. Analyze the patient's notes and provide information useful for Clinical Classifications Software Refined (CCSR) categorization. Here are your instructions:

                    - Highlight conditions, symptoms, medical history, and any other information that can be mapped to specific CCSR categories.

                    - Keep in mind that the CCSR is used for grouping a large number of diseases into manageable categories for statistical analysis and reporting. 

                    - Ensure the conversation includes information that can guide the mapping to CCSR categories, such as the type of disease, cause, location in the body, and patient's age and sex. 

                    - Avoid providing medical advice or diagnostic information. 

                    - Ensure the output is in markdown bullet point format for clarity.

                    - Encourage the user to consult a healthcare professional for advice."""
                },
                {"role": "user", "content": "Can you analyze patient 123's notes and provide useful information for CCSR categorization? Output in markdown bullet points."},
                {
                    "role": "function",
                    "name": function_name,
                    "content": function_response,
                },
            ],
        )
        return second_response

print(run_conversation()["choices"][0]["message"]["content"])
