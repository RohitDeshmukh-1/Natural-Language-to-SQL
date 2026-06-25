# LLM Natural Language to SQL Service

This is my implementation of the natural language to SQL service for the Sparkline database. It takes a plain-English question, translates it to a safe SQL query using Groq's Llama 3.3 70B model, executes the query against `sparkline_demo.db`, and returns the query results along with a plain-English explanation.

---

## Submission Details

I will reply to the assignment email with a single, self-contained ZIP file named `LLM_Rohit.zip`. 

### Required Folder Structure
The ZIP file contains the following structure exactly as requested:
```text
LLM_Rohit/
├── README.md
├── requirements.txt
├── sparkline_demo.db (unchanged)
└── code/
    ├── main.py
    ├── llm.py
    ├── sql_validator.py
    ├── test_app.py
    └── static/
```
Note: Virtual environments, dependency folders, and API keys are excluded from this submission.

---

## Setup & Usage

### 1. Prerequisites
- **Python 3.10+** (developed and tested on Python 3.13.7)
- **Groq API Key** (to perform LLM inference)

### 2. Environment Variables
I configure the application using environment variables. These can be set in a `.env` file in the root `LLM_Rohit/` directory or exported directly in your terminal:
- `GROQ_API_KEY`: **(Required)** Your Groq API key (e.g., `gsk_...`).
- `API_KEY`: The authorization token required to call the API. Defaults to `sparkline-dev-key-123` if not provided.
- `DB_PATH`: Path to the SQLite database. Defaults to `../sparkline_demo.db` relative to the `code/` folder.

### 3. Installation & Running
From the `LLM_Rohit/` root directory, run the following commands to install dependencies, configure the environment, and start the service:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment (edit the .env file and add your GROQ_API_KEY)
cp .env.example .env

# 3. Start the application
cd code
uvicorn main:app --reload --port 8000
```

### 4. Running the Test Suite
To run the automated tests (which cover validator rules and endpoint mock tests):
```bash
pip install -r requirements-dev.txt
cd code
pytest -v
```

### 5. Exercising the API
I implemented authentication via the `X-API-Key` header. Below is how you can call the API using the default development key `sparkline-dev-key-123`:

#### Example `curl` command:
```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sparkline-dev-key-123" \
  -d '{"question": "Who are our top 5 customers by revenue?"}'
```

#### Interactive UI:
You can also open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. I built a modern, responsive web dashboard where you can easily submit questions to the API without using curl.

---

## Sample Runs

Below are three real request/response pairs copied directly from my local testing against the database:

### (a) A Normal Question
**Request:**
```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sparkline-dev-key-123" \
  -d '{"question": "Who are our top 5 customers by revenue?"}'
```
**Response:**
```json
{
  "question": "Who are our top 5 customers by revenue?",
  "sql": "SELECT c.name, SUM(s.amount) as total_revenue FROM sales s JOIN customers c ON s.customer_id = c.id GROUP BY c.name ORDER BY total_revenue DESC LIMIT 5",
  "tables_used": [
    "sales",
    "customers"
  ],
  "result": [
    {
      "name": "HCL Infosystems",
      "total_revenue": 488000.0
    },
    {
      "name": "Flipkart Wholesale",
      "total_revenue": 376000.0
    },
    {
      "name": "Croma Retail",
      "total_revenue": 334000.0
    },
    {
      "name": "Reliance Digital",
      "total_revenue": 311500.0
    },
    {
      "name": "Govt IT Department",
      "total_revenue": 228000.0
    }
  ],
  "answer": "Our top 5 customers by revenue are HCL Infosystems, Flipkart Wholesale, Croma Retail, Reliance Digital, and Govt IT Department, with revenues of 488000, 376000, 334000, 311500, and 228000, respectively."
}
```

### (b) A Grouping or Total Question
**Request:**
```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sparkline-dev-key-123" \
  -d '{"question": "What is total revenue broken down by product category?"}'
```
**Response:**
```json
{
  "question": "What is total revenue broken down by product category?",
  "sql": "SELECT p.category, SUM(s.amount) AS total_revenue FROM sales s JOIN products p ON s.product_id = p.id GROUP BY p.category LIMIT 200",
  "tables_used": [
    "sales",
    "products"
  ],
  "result": [
    {
      "category": "Component",
      "total_revenue": 189000.0
    },
    {
      "category": "Laptop",
      "total_revenue": 1289000.0
    },
    {
      "category": "Monitor",
      "total_revenue": 720000.0
    },
    {
      "category": "Peripheral",
      "total_revenue": 61000.0
    }
  ],
  "answer": "The total revenue is $189,000 for Component, $1,289,000 for Laptop, $720,000 for Monitor, and $61,000 for Peripheral."
}
```

### (c) A Correctly Refused Request (Data Modification Attempt)
**Request:**
```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sparkline-dev-key-123" \
  -d '{"question": "Delete the first sales record"}'
```
**Response:**
```json
{
  "question": "Delete the first sales record",
  "sql": null,
  "tables_used": [],
  "result": [],
  "answer": "Sorry, I can't answer that with the data available (customers, products, sales, and employees)."
}
```
*Note: The model correctly identified this as a data-modification request and refused it at the LLM level (returning `NOT_SUPPORTED`). Had the model generated a destructive query, my `sql_validator.py` or the read-only SQLite database connection layer would have intercepted and blocked it anyway.*

---

## Design Decisions

### 1. Architecture and Request Flow
The request flow for the `POST /ask` endpoint is as follows:
```
[Client] 
   │ (HTTP POST with X-API-Key and Question)
   ▼
[API Auth Dependency] (Checks API Key)
   │
   ▼
[LLM SQL Generator] (Prompts Llama-3.3-70B with Schema and Question to generate SELECT)
   │
   ▼
[SQL Validator] (Checks for stacked queries, readonly SELECT statements, whitelisted tables, and injects LIMIT 200)
   │
   ▼
[SQLite DB Engine] (Executes query using a read-only mode connection mode=ro)
   │
   ▼
[LLM Summarizer] (Summarizes raw JSON results into a natural language sentence)
   │
   ▼
[Client] (Returns final response containing: question, sql, tables_used, result rows, and answer summary)
```

I split the workflow into two distinct LLM calls:
1. **SQL Generation:** Translating natural language to a query.
2. **Answer Summarization:** Generating the final sentence *after* the query is executed. 

By running the query first and passing the exact database output to the summarizer, the service avoids LLM hallucinations regarding numbers or records.

### 2. LLM / Provider Choice
I chose **Groq** as the provider, running the **Llama 3.3 70B** model (`llama-3.3-70b-versatile`). It offers ultra-low latency inference, high quality instruction-following for SQL syntax, and standard OpenAI-compatible client libraries.

### 3. Validation Strategy
Security is the key pillar of this service. I designed a multi-layer validation pipeline in `sql_validator.py` to prevent SQL injection and malicious queries:
- **Comments Stripping:** Before parsing, SQL comments (`--` and `/* ... */`) are stripped. This blocks attempts to hide stacked commands behind comment syntax.
- **Single Statement Enforcement:** The SQL is parsed with `sqlparse` to guarantee it contains exactly one statement.
- **Statement Type Check:** The parser ensures the statement is a read-only `SELECT` (or a `WITH ... SELECT` common table expression).
- **Keyword Blocklist:** An independent keyword blocklist (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `PRAGMA`, etc.) acts as a secondary layer of protection.
- **Table Whitelist:** The system introspects the database at startup to find all valid tables (`customers`, `products`, `sales`, `employees`). The SQL is parsed to extract all table names, and any query referencing non-whitelisted tables (or system tables like `sqlite_master`) is rejected.
- **Automatic Limit Ingestion:** If a query doesn't specify a `LIMIT`, the validator appends `LIMIT 200` to prevent memory exhaustion from massive data dumps.
- **Database-Level Read-Only Mode:** As a final line of defense, the SQLite connection is established using `file:../sparkline_demo.db?mode=ro` with `PRAGMA query_only = ON`. Even if a malicious command bypasses the parser, SQLite will reject the write operation at the engine level.

### 4. Authentication Approach
I used a simple and secure API key authentication pattern. Clients must send the header `X-API-Key` with their requests. A FastAPI dependency verifies the key against the configured environment variable, returning a `401 Unauthorized` for invalid or missing keys.

---

## Prompt Design

I used two tailored prompts with `temperature=0` to ensure deterministic, consistent behavior:

### 1. SQL Generation Prompt (System Prompt)
```text
You are a SQL generator for a read-only business reporting tool. You write SQLite SELECT queries based on a plain-English question.

Database schema:
{schema}

Rules you must follow exactly:
- Only ever write a single SELECT statement (a WITH ... SELECT is fine too). Never write INSERT, UPDATE, DELETE, DROP, ALTER, or any statement that changes data.
- Only use the tables and columns listed in the schema above. Never invent a table or column name.
- Use SQLite syntax (e.g. strftime for dates).
- Do not add a trailing semicolon.
- Output ONLY the raw SQL query - no markdown code fences, no explanation, no comments.
- If the question genuinely cannot be answered with the tables above, output exactly: NOT_SUPPORTED
```
* **Reasoning:** Stating the rules explicitly (and repeating the SELECT-only rule) keeps the model highly aligned with the safety requirements. The `{schema}` section is dynamically compiled at startup from actual database reflection (using `PRAGMA` tables and columns) to prevent schema-drift. The `NOT_SUPPORTED` sentinel gives the model a clear out, avoiding hallucinated SQL or invalid queries for questions outside the data scope.

### 2. Answer Summarization Prompt (System Prompt)
```text
You explain database query results in plain English for a non-technical business user. You will be given the original question, the SQL that was run, and the actual result rows. Write exactly one short, natural sentence that answers the question using only the numbers/values present in the result. Never invent a number that isn't in the result. If the result is empty, say plainly that no matching data was found. Do not mention SQL or tables.
```
* **Reasoning:** Instructing the model to use *only* the provided JSON result rows prevents it from inventing numbers, hallucinating facts, or using its training cutoff data to answer questions about the database.

---

## Assumptions

- **Fixed Database Schema:** I assumed the database structure of `sparkline_demo.db` is stable at runtime. While the service reflects the schema dynamically at startup, it does not watch the database file for runtime schema migrations.
- **Shared API Key:** I assumed a single shared key is sufficient for authenticating external calls in this environment, rather than a full multi-tenant RBAC system.
- **Read-Only Scope:** I assumed "read-only" means SELECT-only at the database engine level. All authenticated callers are granted read permissions to all columns and tables.
- **Table Relationships:** I observed that the `employees` table does not have a join key linking it to `sales` (e.g., `employee_id` is missing in `sales`). I assumed that queries asking for sales broken down by employees are not answerable, and the model should output `NOT_SUPPORTED` rather than guessing a relationship.
- **Table Extraction Logic:** I assumed that regex-based table extraction (`FROM`/`JOIN` parsing) is sufficient for the simplified reporting queries generated by the LLM.

---

## Limitations & Future Improvements

- **Single Shared API Key:** In a production system, I would implement multi-tenant API key management (stored in a secure database hash) with individual rate limits and scopes.
- **Lack of Conversation State:** The endpoint is currently stateless. I would add a session-based memory manager (e.g., using Redis) so users can ask contextual follow-up questions (e.g., "What was the total?" followed by "How much of that was from laptops?").
- **Regex-Based SQL Parsing:** The current regex table extractor works well for standard queries but might struggle with highly nested subqueries. I would replace it with a comprehensive AST-based SQL parser (like `sqlglot`) to extract dependencies more robustly.
- **Caching Layer:** I would introduce a cache (e.g., Redis or in-memory LRU cache) for identical question-to-SQL results to reduce Groq API calls and speed up response times for common dashboards.
- **Statement Timeout Guard:** While `LIMIT` protects memory, a complex or poorly indexed query could cause CPU execution timeouts. I would add query timeout configurations at the SQLite level to prevent resource exhaustion attacks.

---

## Tools and Resources Used

- **Groq Python SDK:** For executing chat completions with Llama 3.3.
- **sqlparse:** Used for SQL comment removal and statement tokenization.
- **FastAPI / Uvicorn:** For constructing the API and hosting the server.
- **python-dotenv:** For simple environment configuration loading.

### Modified / Rejected Approach
I initially considered using the LLM itself to return the list of `tables_used` (either via tool calls or JSON output formats). However, I rejected this approach because the LLM's self-reported list is prone to inconsistencies and hallucinations. Instead, I parsed the final SQL query in Python code. Since the SQL query is the absolute truth of what SQLite runs, extracting the tables directly from the SQL text is completely deterministic and 100% reliable.

---

## One-Line Note

**LLM/Provider/Approach Used:** Groq API running Llama 3.3 70B (`llama-3.3-70b-versatile`) via Python's `groq` package, validated with a multi-layered Python-based SQL parser (`sqlparse`) and run on a read-only SQLite connection.
