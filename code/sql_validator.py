"""
Everything to do with making sure SQL coming back from the LLM is safe to run.

The basic idea: never trust the SQL string an LLM gives you. It's just text
the model generated based on a prompt, and prompts can be worded in odd ways,
models can misfire, and (in a real product) a user could try to manipulate
the question itself to get the model to emit something destructive. So this
file treats whatever comes out of llm.py exactly like untrusted user input,
even though it technically came from "our own" model call.

Checks done here, in order:
  1. strip out comments, because someone could hide a second statement after
     a "--" comment and a naive check would miss it
  2. make sure there's exactly one SQL statement (blocks query stacking like
     "SELECT 1; DROP TABLE customers")
  3. make sure that one statement is a SELECT (or a WITH ... SELECT), nothing else
  4. scan for a blocklist of dangerous keywords as a second layer, in case
     sqlparse's statement type detection ever gets fooled by something weird
  5. pull out every table name the query touches and make sure all of them
     are tables we actually know about - this also catches the LLM trying
     to query sqlite_master or some other system table to snoop the schema
  6. cap how many rows can come back, so a careless "SELECT * FROM sales"
     style query without a WHERE/LIMIT can't dump the whole table

None of these checks are exotic, on purpose - this is meant to be the kind
of validation logic a reviewer can read top to bottom in two minutes and
understand exactly what it does and doesn't catch.
"""
import re
import sqlparse

MAX_ROWS = 200

# Anything in here showing up as a standalone keyword is an instant reject.
# This is a belt-and-suspenders check on top of the "is it a SELECT" check -
# even inside a SELECT, none of these words have a legitimate reason to appear.
FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE",
    "TRUNCATE", "ATTACH", "DETACH", "PRAGMA", "GRANT", "REVOKE", "VACUUM",
    "REINDEX", "EXEC", "EXECUTE", "BEGIN", "COMMIT", "ROLLBACK",
]


class SQLValidationError(Exception):
    """Raised whenever a query fails a safety check, with a human-readable reason."""
    pass


def _strip_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments before doing anything else."""
    sql = re.sub(r"--.*?(\n|$)", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql.strip()


def _check_single_statement(sql: str) -> str:
    """Returns the single statement if there's exactly one, otherwise raises."""
    statements = [s for s in sqlparse.split(sql) if s.strip()]
    if len(statements) == 0:
        raise SQLValidationError("The query came back empty.")
    if len(statements) > 1:
        raise SQLValidationError("Only a single SQL statement is allowed at a time.")
    return statements[0]


def _check_is_select(sql: str) -> None:
    """Confirms the statement is a SELECT, allowing a WITH (CTE) prefix."""
    parsed = sqlparse.parse(sql)[0]
    stmt_type = parsed.get_type()  # sqlparse's guess: SELECT, INSERT, UNKNOWN, etc.

    if stmt_type == "SELECT":
        return

    # sqlparse sometimes tags "WITH ... SELECT ..." as UNKNOWN, so check manually
    first_token = parsed.token_first(skip_cm=True)
    if first_token and first_token.ttype is sqlparse.tokens.CTE:
        # crude but effective: a WITH query is only safe if it eventually selects
        if re.search(r"\bSELECT\b", sql, re.IGNORECASE):
            return

    raise SQLValidationError(
        f"Only read-only SELECT queries are allowed (this looked like '{stmt_type}')."
    )


def _check_forbidden_keywords(sql: str) -> None:
    for word in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{word}\b", sql, re.IGNORECASE):
            raise SQLValidationError(f"The keyword '{word}' isn't allowed in generated queries.")


def extract_tables_used(sql: str) -> list[str]:
    """
    Pulls out table names referenced after FROM / JOIN.
    Used both as a safety check (only known tables allowed) and to fill in
    the "tables_used" field in the API response - the assignment specifically
    asks for that to be derived from the SQL itself, not reported by the LLM.
    """
    sql_no_comments = _strip_comments(sql)
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
    found = pattern.findall(sql_no_comments)
    # dedupe while keeping order, and lowercase since SQLite table names are
    # case-insensitive for matching purposes
    seen = []
    for t in found:
        t_lower = t.lower()
        if t_lower not in seen:
            seen.append(t_lower)
    return seen


def _extract_cte_names(sql: str) -> set[str]:
    """
    Finds names defined by a WITH clause, e.g. "WITH recent AS (...)" -> {"recent"}.
    These are query-local aliases, not real tables, so they need to be allowed
    through the table whitelist check without actually being in the database.
    """
    names = set()
    for match in re.finditer(r"\bWITH\b(.*?)\bSELECT\b", sql, re.IGNORECASE | re.DOTALL):
        cte_block = match.group(1)
        for name_match in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", cte_block, re.IGNORECASE):
            names.add(name_match.group(1).lower())
    return names


def validate_sql(sql: str, known_tables: set[str]) -> str:
    """
    Runs the full pipeline and returns a cleaned-up, safe-to-execute SQL string.
    Raises SQLValidationError with a clear message if anything looks off.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("No SQL was generated.")

    cleaned = _strip_comments(sql)
    statement = _check_single_statement(cleaned)
    _check_is_select(statement)
    _check_forbidden_keywords(statement)

    tables = extract_tables_used(statement)
    if not tables:
        raise SQLValidationError("Couldn't figure out which table(s) this query touches.")

    cte_names = _extract_cte_names(statement)
    allowed = known_tables | cte_names
    unknown = [t for t in tables if t not in allowed]
    if unknown:
        raise SQLValidationError(f"Query references unknown table(s): {', '.join(unknown)}")

    # cap result size - add a LIMIT if the query doesn't already have one
    if not re.search(r"\bLIMIT\s+\d+", statement, re.IGNORECASE):
        statement = statement.rstrip().rstrip(";") + f" LIMIT {MAX_ROWS}"

    return statement
