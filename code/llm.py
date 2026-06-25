"""
Everything that talks to the LLM lives here. Two jobs, two functions:

  1. generate_sql()   - turns the user's English question into a SQL query
  2. summarize_result() - turns the actual query result into one plain
                           English sentence

These are deliberately kept as two separate calls instead of one. The model
writing the SQL has never seen the real numbers (it can't have - the query
hasn't run yet), so asking it to also write the final answer in the same
call would mean it's guessing at the answer instead of reading it off the
real result. Splitting it in two means the "answer" sentence is always
grounded in data that actually came out of SQLite, not something the model
imagined.

Using Groq's API here because it's fast and has an OpenAI-compatible style
client (the `groq` package), running Llama 3.3 70B (model id
"llama-3.3-70b-versatile" at the time of writing).
"""
import os
import json
import sqlite3
from groq import Groq

MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

_client = None


def get_client() -> Groq:
    """Lazy init so importing this module doesn't blow up if the key isn't set yet."""
    global _client
    if _client is None:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY environment variable is not set.")
        _client = Groq(api_key=api_key)
    return _client


def describe_schema(db_path: str) -> tuple[str, set[str]]:
    """
    Reads the actual schema out of the SQLite file (table names, columns,
    types, and foreign keys) and turns it into a short text block to hand
    to the model as context.

    Doing this by introspection instead of hardcoding the schema in a string
    means the prompt can never drift out of sync with the real database -
    if the table changes, this just picks it up next request.

    Returns the schema text plus a set of known table names (the validator
    uses that set to reject queries against tables that don't exist).
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in cur.fetchall()]

    lines = []
    for table in tables:
        cur.execute(f"PRAGMA table_info({table})")
        cols = [f"{row[1]} {row[2]}" for row in cur.fetchall()]
        lines.append(f"{table}({', '.join(cols)})")

        cur.execute(f"PRAGMA foreign_key_list({table})")
        for fk in cur.fetchall():
            # fk columns: id, seq, table, from, to, ...
            lines.append(f"  - {table}.{fk[3]} references {fk[2]}.{fk[4]}")

    conn.close()
    return "\n".join(lines), set(tables)


SQL_SYSTEM_PROMPT = """You are a SQL generator for a read-only business reporting tool. \
You write SQLite SELECT queries based on a plain-English question.

Database schema:
{schema}

Rules you must follow exactly:
- Only ever write a single SELECT statement (a WITH ... SELECT is fine too). \
Never write INSERT, UPDATE, DELETE, DROP, ALTER, or any statement that changes data.
- Only use the tables and columns listed in the schema above. Never invent a table or column name.
- Use SQLite syntax (e.g. strftime for dates).
- Do not add a trailing semicolon.
- Output ONLY the raw SQL query - no markdown code fences, no explanation, no comments.
- If the question genuinely cannot be answered with the tables above, output exactly: NOT_SUPPORTED
"""

ANSWER_SYSTEM_PROMPT = """You explain database query results in plain English for a non-technical \
business user. You will be given the original question, the SQL that was run, and the actual \
result rows. Write exactly one short, natural sentence that answers the question using only the \
numbers/values present in the result. Never invent a number that isn't in the result. If the \
result is empty, say plainly that no matching data was found. Do not mention SQL or tables."""


def _strip_code_fence(text: str) -> str:
    """Models love wrapping output in ```sql ... ``` even when told not to - strip it if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("sql"):
            text = text[3:]
    return text.strip().rstrip(";").strip()


def generate_sql(question: str, schema_text: str) -> str:
    """Calls the model once to turn the question into SQL. Returns the raw SQL string,
    or the literal string 'NOT_SUPPORTED' if the model says the question can't be answered."""
    client = get_client()
    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,  # we want consistent SQL, not creative SQL
        messages=[
            {"role": "system", "content": SQL_SYSTEM_PROMPT.format(schema=schema_text)},
            {"role": "user", "content": question},
        ],
    )
    raw = response.choices[0].message.content
    return _strip_code_fence(raw)


def summarize_result(question: str, sql: str, rows: list[dict]) -> str:
    """Calls the model a second time, this time with the real result rows, to get
    one grounded plain-English sentence answering the original question."""
    client = get_client()
    user_content = (
        f"Question: {question}\n"
        f"SQL used: {sql}\n"
        f"Result rows (JSON): {json.dumps(rows)}"
    )
    response = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content.strip()
