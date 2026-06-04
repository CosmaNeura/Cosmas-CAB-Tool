import pandas as pd

# Read the CSV file
# df = pd.read_csv('DXCCSR_v2023-1.csv')

# Convert the DataFrame to a Feather file
# df.to_feather('DXCCSR_v2023-1.feather')

# Access the first data from the Feather file
billing_df_feather = pd.read_feather('datasets/DXCCSR/DXCCSR_v2023-1.feather')
ccsr_df_feather = pd.read_feather('datasets/DXCCSR/DXCCSR-Reference-File-v2023-1.feather')

# print(ccsr_df_feather)

# create embeddings of all of the 'CCSR Category Description' values, then create a vector database of the 'CCSR Category' , 'CCSR Category Description' and the embedding vector 

import os
from dotenv import load_dotenv
import pinecone
import openai

# Set OpenAI API Key
load_dotenv()
openai.api_key = os.environ["OPENAI_API_KEY"]

# Define function to get OpenAI embedding
def gpt3_embedding(content, engine='text-embedding-ada-002'):
    content = content.encode(encoding='ASCII',errors='ignore').decode() 
    response = openai.Embedding.create(input=content,engine=engine) 
    vector = response['data'][0]['embedding']  
    return vector


# # Generate embeddings for the 'CCSR Category Description' texts
# ids = ccsr_df_feather['CCSR Category'].tolist()
# embeddings = [gpt3_embedding(desc) for desc in ccsr_df_feather['CCSR Category Description']]

# # Add the embeddings to the feather file
# ccsr_df_feather['Embeddings'] = embeddings
# ccsr_df_feather.to_feather('DXCCSR-Reference-File-v2023-1.feather')

# Test Example Input 
transcribed_text = """
            "patient": "Hello doctor,My son aged 13 years has got his left eye -11 and right with -8. I am now very worried what good precautions should I take with him. Can you suggest me what should I do?",
            "physician": "Hello, Welcome to Chat Doctor forum. I have gone through what you have mentioned and understand your anxiety about your son\\s eyesight. - 11D and - 8D are high myopic powers and the chances are that the power may progress as your son is still young. My advice to you is: 1. Avoid reading or holding objects very near to the eye. This will cause myopia to progress faster and will worsen the condition. 2. Every six monthly visits or follow up visits as per doctors advise. Since the myopia is of a high degree, there will be excessive stretching and degeneration of the structures of the eyeball. This can cause problems such as retinal detachment. Regular follow-ups help us in picking up changes faster and in preventing such complications as retinal detachment. 3. Ask your son to regularly self-check his vision. This is done by closing each eye one at a time and observing how a particular object or letter looks. In case he is able to pick up any change or  ChatDoctor.  4. Urgently visit your doctor in case of a sudden ",
"""

import numpy as np
from numpy.linalg import norm

def similarity(v1, v2):  # return dot product of two vectors
    return np.dot(v1, v2)/(norm(v1)*norm(v2)) #dot product is a measure of similarity between two vectors


def search_index(text, data, count=5):
    vector = gpt3_embedding(text)  # get vector data for the question
    scores = []
    for i, content in zip(data, ccsr_df_feather['CCSR Category Description']):
        score = similarity(vector, i)  # compare vector data for the question versus each embedding
        scores.append({'content': content, 'score': score})  # create similarity scores for each document
    ordered = sorted(scores, key=lambda d: d['score'], reverse=True)  # in scores list, sort 0th "score" data -to- last "score" data by highest to lowest
    return ordered[:count]  # return top count documents


ccsr_categories_list = search_index(transcribed_text, ccsr_df_feather['Embeddings'].tolist())
