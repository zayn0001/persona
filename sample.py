import ast
import datetime
import json
import os
import re
import tempfile
from fastapi.responses import JSONResponse
import openai
from pydantic import BaseModel
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import uvicorn
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
load_dotenv()

app = FastAPI()    
client = openai.OpenAI()

mongoclient = MongoClient(os.getenv("MONGODB_URI"), server_api=ServerApi('1'))
db = mongoclient['persona']
collection = db['userdata']
    

def _generate_tags(journal_entry):
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    my_assistant = client.beta.assistants.create(
        instructions="You are tag generator for journal entries",
        name="testing",
        model="gpt-4o",
    )
    thread = client.beta.threads.create()
    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=my_assistant.id,
        model="gpt-4o",
        instructions= """You will be provided with a journal entry. Create tags for the journal entry based on what it talks about. The tags should not be extremely specific and must include some important aspects like the phase of life, the emotion it conveys, the impace it makes along with anything extra. Similar to the the following example
        Input: When i was 22, i had a huge breakup with a partner of 7 years and that took a huge toll on my mental health and changed my view on relationships ever since.
        Output: ['romantic relationship', 'struggle', 'mental health', 'sad', 'teen'].
        Create tags for the following question: \n""" + journal_entry
        )
    if run.status == "completed":
        messages = client.beta.threads.messages.list(
            thread_id=thread.id,
            run_id=run.id
        )
        text = messages.data[0].content[0].text.value
        
        #Remove citations
        text = re.sub(r'【.*?】', '', text) 

        #extract list
        match = re.search(r'\[([^\[\]]*)\]', text)
        if match:
            print("found list")
            list_string = match.group(1)
            text = ast.literal_eval(list_string)
            text = list(text)

        # Delete the assistant and the thread after use
        client.beta.assistants.delete(assistant_id=my_assistant.id)
        client.beta.threads.delete(thread_id=thread.id)
        return text 


def get_entries_as_json(username: str) -> str:
    # Query the MongoDB collection to get the user's entries
    user_data = collection.find_one({"username": username}, {"_id": 0, "entries": 1})
    
    if not user_data or 'entries' not in user_data:
        raise ValueError("No entries found for the given username")

    # Serialize the entries to a JSON formatted string
    entries_json = json.dumps(user_data['entries'], indent=4)

    # Create a temporary file to store the JSON data
    with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as temp_file:
        temp_file.write(entries_json.encode('utf-8'))
        temp_filename = temp_file.name
    
    return temp_filename

class QuestionRequest(BaseModel):
    question: str
    username: str
@app.post("/ask")
async def ask_question(request: QuestionRequest):
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    user_data = collection.find_one({"username": request.username}, {"_id": 0, "vector_store_id": 1})
    print(user_data["vector_store_id"])
    my_assistant = client.beta.assistants.create(
        instructions="",
        name="testing",
        model="gpt-4o",
        tools=[{"type": "file_search"}],
        tool_resources={
            "file_search": {
                "vector_store_ids": [user_data["vector_store_id"]]
            }
        }
    )
    thread = client.beta.threads.create()

    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=my_assistant.id,
        model="gpt-4o",
        instructions= "you have been provided with journal entries of myself. answer questions about myself regarding my life using the data provided. Talk in the second person. The question is as follows: " + request.question
        )
    if run.status == "completed":
        messages = client.beta.threads.messages.list(
            thread_id=thread.id,
            run_id=run.id
        )
        text = messages.data[0].content[0].text.value
        #text = extractJson(text)
        text = re.sub(r'【.*?】', '', text) # Remove unwanted stuff from the text

        # Delete the assistant and the thread after use
        client.beta.assistants.delete(assistant_id=my_assistant.id)
        client.beta.threads.delete(thread_id=thread.id)
        return JSONResponse(content={"answer":text})
    



class JournalEntryRequest(BaseModel):
    username: str
    entry: str
@app.post("/add_entry")
async def add_journal_entry(request: JournalEntryRequest):
    # Generate tags for the journal entry
    tags = _generate_tags(request.entry)
            
    new_entry = {
        "content": request.entry,
        "tags": tags,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat()
    }

    user = collection.find_one({"username": request.username})

    if user:
        vector_store_id = user["vector_store_id"] 

        collection.update_one(
        {"username": request.username},
        {"$push": {"entries": new_entry}}
        )

        vector_store_files = client.beta.vector_stores.files.list(vector_store_id=vector_store_id)
        vector_store_file = list(vector_store_files)[0]
        client.beta.vector_stores.files.delete(vector_store_id=vector_store_id,file_id=vector_store_file.id)
        client.files.delete(vector_store_file.id)

        file_path = get_entries_as_json(username=request.username)
        client_file_id =  client.files.create(file=open(file_path, "rb"),purpose="assistants").id
        client.beta.vector_stores.files.create(vector_store_id=vector_store_id, file_id=client_file_id)

    else:
        vector_store_id = client.beta.vector_stores.create().id

        collection.insert_one({
            "username": request.username,
            "entries": [new_entry],
            "vector_store_id":vector_store_id
        })

        file_path = get_entries_as_json(username=request.username)
        client_file_id =  client.files.create(file=open(file_path, "rb"),purpose="assistants").id
        client.beta.vector_stores.files.create(vector_store_id=vector_store_id, file_id=client_file_id)


    return JSONResponse(content={"message": "Journal entry added successfully", "entry": new_entry})


#I got a promotion at work today after 2 years. My work life balance has been great and I'm happy to be working with great people and at a company i love. My coworkers surprised me later that night with a cake and drinks on them.

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
