from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials
from fastapi.middleware.cors import CORSMiddleware
import json, os, secrets
from dotenv import load_dotenv

import expense_text
import firebase_store

load_dotenv()

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

SHEET_ID = "1IDBkc4Lh8SueHu3_GMjeybm92Xwiefc7LfeQwsQd-sY"

_sheet = None


def get_sheet():
    """Open the Sheets tab lazily so the app still boots without credentials."""
    global _sheet
    if _sheet is not None:
        return _sheet
    # Load credentials from env variable
    # this is for local testing
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    # this is for deployment
    # creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    # creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    # client = gspread.authorize(creds)
    month = datetime.now().strftime("%B")
    _sheet = client.open_by_key(SHEET_ID).worksheet(month)
    return _sheet


class Expense(BaseModel):
    date: str
    category: str
    description: str
    amount: float


class QuickExpense(BaseModel):
    text: str


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/add-expense")
def add_expense(expense: Expense):
  try:
    get_sheet().append_row([expense.date, expense.description, expense.amount,expense.category])
    return {"status": "success", "data": expense}
  except:
    return {"status": "failed", "data": ""}


def _require_api_key(provided: str | None):
    expected = os.environ.get("QUICK_EXPENSE_API_KEY")
    if not expected:
        # Fail closed: without a configured key this endpoint stays unusable.
        raise HTTPException(status_code=503, detail="QUICK_EXPENSE_API_KEY not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.post("/quick-expense")
def quick_expense(body: QuickExpense, x_api_key: str | None = Header(default=None)):
    """Accept dictated text like "450 Groceries" and store it in Firebase."""
    _require_api_key(x_api_key)

    tz_name = os.environ.get("EXPENSE_TIMEZONE", "Asia/Kolkata")
    try:
        record = expense_text.parse_expense_text(body.text, tz_name=tz_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        key = firebase_store.add_expense(
            record, os.environ.get("EXPENSE_DATA_ROOT", "")
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Spoken back by the iPhone shortcut, so keep it short and human.
    return {
        "status": "success",
        "id": key,
        "message": f"Added {record['amount']} to {record['category']}",
        "data": record,
    }
