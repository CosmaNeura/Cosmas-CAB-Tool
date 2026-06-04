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
input_text = """
- Patient ID: 123
- Age: 26 years
- Gender: Male
- Height: 5 feet 9 inches
- Weight: 255 pounds
- Symptoms:
  - Pooping blood after eating spicy food
  - Occasional blood in stool during constipation
  - Frequent diarrhea
- Concerns:
  - Fear of having colon cancer
- Family history: No history of colon cancer
- Reported getting blood tests done with reports attached
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


ccsr_categories_list = search_index(input_text, ccsr_df_feather['Embeddings'].tolist())
