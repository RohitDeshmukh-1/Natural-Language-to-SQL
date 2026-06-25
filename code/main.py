"""
The actual API. Run with:
    uvicorn main:app --reload

What happens on a request to POST /ask:
  1. check the X-API-Key header
  2. ask the LLM to turn the question into SQL          (llm.generate_sql)
  3. validate that SQL before going anywhere near it     (sql_validator.validate_sql)
  4. run it against a read-only SQLite connection
  5. ask the LLM for a one-line plain-English summary of the real result
  6. return everything as JSON

Every failure point (bad key, LLM down, invalid SQL, empty result, db error)
is caught and turned into a sensible JSON response instead of a 500 crash.

The app also serves a lightweight web UI at / so reviewers can test the
API interactively in the browser without needing curl.
"""
import os
import sqlite3
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

import llm
from sql_validator import validate_sql, extract_tables_used, SQLValidationError

load_dotenv()  # picks up a .env file in the working directory, if one exists - no-op otherwise

DB_PATH = os.environ.get("DB_PATH", "../sparkline_demo.db")
API_KEY = os.environ.get("API_KEY", "sparkline-dev-key-123")

app = FastAPI(title="Sparkline NL-to-SQL Service")

# Allow the browser-based UI to call the API from the same origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the web UI
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Schema is read once at startup rather than on every request - it doesn't
# change while the server is running, and re-reading it each time would
# just be wasted work (and an extra few hundred ms on every request).
SCHEMA_TEXT, KNOWN_TABLES = llm.describe_schema(DB_PATH)


class AskRequest(BaseModel):
    question: str


def check_api_key(x_api_key: str = Header(default=None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")


def run_select(sql: str) -> list[dict]:
    """
    Executes a validated SELECT against the database in read-only mode.

    The `mode=ro` URI flag plus `PRAGMA query_only = ON` are a second,
    connection-level safety net on top of the sql_validator checks - even
    if a bad query somehow slipped past validation, SQLite itself will
    refuse to let this connection write anything.
    """
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON;")
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


@app.get("/")
def root():
    """Serve the interactive web UI."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/ask")
def ask(request: AskRequest, x_api_key: str = Header(default=None)):
    check_api_key(x_api_key)

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question can't be empty.")

    # Step 1: NL -> SQL
    try:
        raw_sql = llm.generate_sql(question, SCHEMA_TEXT)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach the LLM: {e}")

    if raw_sql.strip().upper() == "NOT_SUPPORTED":
        return {
            "question": question,
            "sql": None,
            "tables_used": [],
            "result": [],
            "answer": "Sorry, I can't answer that with the data available (customers, "
                      "products, sales, and employees).",
        }

    # Step 2: validate
    try:
        safe_sql = validate_sql(raw_sql, KNOWN_TABLES)
    except SQLValidationError as e:
        # this is the "correctly refused" path - we still return 200 with a
        # clear reason rather than crashing, since this is an expected
        # outcome of the system working correctly, not a server error
        return {
            "question": question,
            "sql": raw_sql,
            "tables_used": [],
            "result": [],
            "answer": f"This request was refused for safety reasons: {e}",
        }

    # Step 3: execute
    try:
        rows = run_select(safe_sql)
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"Database error while running the query: {e}")

    tables_used = extract_tables_used(safe_sql)

    # Step 4: plain-English answer, grounded in the real rows
    try:
        answer = llm.summarize_result(question, safe_sql, rows)
    except Exception:
        # if the summary call fails we still have a perfectly good result -
        # no reason to fail the whole request over a wording step
        answer = "Here are the matching results (a plain-English summary couldn't be generated)."

    return {
        "question": question,
        "sql": safe_sql,
        "tables_used": tables_used,
        "result": rows,
        "answer": answer,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
