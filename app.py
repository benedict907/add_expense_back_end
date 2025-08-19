from fastapi import FastAPI
from pydantic import BaseModel
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials
from fastapi.middleware.cors import CORSMiddleware
import json, os
from google.oauth2.service_account import Credentials


app = FastAPI()

# 👇 allow frontend to talk to backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or ["http://localhost:5173"] for Vite
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Google Sheets Auth
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Load credentials from env variable
# creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
client = gspread.authorize(creds)

month = datetime.now().strftime("%B")

SHEET_ID = "1IDBkc4Lh8SueHu3_GMjeybm92Xwiefc7LfeQwsQd-sY"
sheet = client.open_by_key(SHEET_ID).worksheet(month)

class Expense(BaseModel):
    date: str
    category: str
    description: str
    amount: float

@app.post("/add-expense")
def add_expense(expense: Expense):
  try:  
    sheet.append_row([expense.date, expense.category, expense.description, expense.amount])
    return {"status": "success", "data": expense}
  except: 
    return {"status": "failed", "data": ""}