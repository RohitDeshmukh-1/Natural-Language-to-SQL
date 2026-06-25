"""
Tests for this service. Two groups:

  - TestValidator: unit tests for sql_validator.py directly, no app or network
    involved at all - just feeding SQL strings in and checking what comes out.
  - TestAskEndpoint: integration tests that go through the real FastAPI app via
    TestClient, with llm.generate_sql / llm.summarize_result mocked out. The
    LLM calls are mocked because hitting the real Groq API in a test suite
    would make tests slow, flaky, and dependent on having a real API key - the
    things actually worth testing here (auth, validation, execution, error
    handling) don't depend on what the LLM specifically says, only on the
    shape of what it returns.

Run with: pytest -v   (from the code/ directory)
"""
import os

os.environ.setdefault("API_KEY", "sparkline-dev-key-123")
os.environ.setdefault("GROQ_API_KEY", "dummy-key-for-tests")

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from sql_validator import validate_sql, extract_tables_used, SQLValidationError
import main

client = TestClient(main.app)
AUTH_HEADERS = {"X-API-Key": "sparkline-dev-key-123"}
KNOWN_TABLES = {"customers", "products", "sales", "employees"}


# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------

class TestValidator:
    def test_simple_select_passes(self):
        sql = "SELECT name FROM customers"
        result = validate_sql(sql, KNOWN_TABLES)
        assert result.startswith("SELECT name FROM customers")

    def test_join_and_group_by_passes(self):
        sql = (
            "SELECT c.name, SUM(s.amount) AS revenue FROM customers c "
            "JOIN sales s ON s.customer_id = c.id GROUP BY c.name"
        )
        result = validate_sql(sql, KNOWN_TABLES)
        assert "JOIN sales" in result

    def test_cte_query_passes(self):
        # this was an actual bug I found while testing manually - the validator
        # was treating the CTE's own alias as an unknown table
        sql = "WITH recent AS (SELECT * FROM sales) SELECT * FROM recent"
        result = validate_sql(sql, KNOWN_TABLES)
        assert result is not None

    def test_adds_limit_when_missing(self):
        result = validate_sql("SELECT * FROM customers", KNOWN_TABLES)
        assert "LIMIT 200" in result

    def test_keeps_existing_limit(self):
        result = validate_sql("SELECT * FROM customers LIMIT 10", KNOWN_TABLES)
        assert result.count("LIMIT") == 1
        assert "LIMIT 10" in result

    @pytest.mark.parametrize("sql", [
        "DELETE FROM sales WHERE id = 1",
        "UPDATE customers SET name = 'x' WHERE id = 1",
        "DROP TABLE customers",
        "INSERT INTO customers (name) VALUES ('x')",
        "ALTER TABLE customers ADD COLUMN x TEXT",
        "PRAGMA table_info(customers)",
    ])
    def test_non_select_statements_rejected(self, sql):
        with pytest.raises(SQLValidationError):
            validate_sql(sql, KNOWN_TABLES)

    def test_statement_stacking_rejected(self):
        with pytest.raises(SQLValidationError):
            validate_sql("SELECT * FROM sales; DROP TABLE customers;", KNOWN_TABLES)

    def test_hidden_statement_after_comment_rejected(self):
        # a naive single-statement check could be fooled by hiding the second
        # statement after a comment - comments need to be stripped first
        with pytest.raises(SQLValidationError):
            validate_sql("SELECT 1 -- ; \n; DROP TABLE customers;", KNOWN_TABLES)

    def test_unknown_table_rejected(self):
        with pytest.raises(SQLValidationError):
            validate_sql("SELECT * FROM made_up_table", KNOWN_TABLES)

    def test_system_table_rejected(self):
        # blocks the LLM from reading the schema directly via sqlite_master
        with pytest.raises(SQLValidationError):
            validate_sql("SELECT * FROM sqlite_master", KNOWN_TABLES)

    def test_empty_sql_rejected(self):
        with pytest.raises(SQLValidationError):
            validate_sql("", KNOWN_TABLES)

    def test_extract_tables_used(self):
        sql = "SELECT * FROM customers c JOIN sales s ON s.customer_id = c.id"
        assert extract_tables_used(sql) == ["customers", "sales"]


# ---------------------------------------------------------------------------
# API integration tests (LLM calls mocked, everything else real)
# ---------------------------------------------------------------------------

class TestAskEndpoint:
    def test_missing_api_key_rejected(self):
        r = client.post("/ask", json={"question": "anything"})
        assert r.status_code == 401

    def test_wrong_api_key_rejected(self):
        r = client.post("/ask", headers={"X-API-Key": "wrong"}, json={"question": "anything"})
        assert r.status_code == 401

    def test_empty_question_rejected(self):
        r = client.post("/ask", headers=AUTH_HEADERS, json={"question": "   "})
        assert r.status_code == 400

    def test_normal_question_returns_real_data(self):
        sql = (
            "SELECT c.name, SUM(s.amount) AS revenue FROM customers c "
            "JOIN sales s ON s.customer_id = c.id GROUP BY c.name "
            "ORDER BY revenue DESC LIMIT 5"
        )
        with patch("main.llm.generate_sql", return_value=sql), \
             patch("main.llm.summarize_result", return_value="Top customer found."):
            r = client.post("/ask", headers=AUTH_HEADERS, json={"question": "top 5 customers"})

        assert r.status_code == 200
        body = r.json()
        assert body["tables_used"] == ["customers", "sales"]
        assert len(body["result"]) == 5
        # this is real data straight out of the actual sqlite file, not mocked
        assert "revenue" in body["result"][0]
        assert body["answer"] == "Top customer found."

    def test_destructive_request_is_refused_not_crashed(self):
        with patch("main.llm.generate_sql", return_value="DELETE FROM sales WHERE id = 1"):
            r = client.post("/ask", headers=AUTH_HEADERS, json={"question": "delete a row"})

        assert r.status_code == 200  # refusal is a normal response, not a server error
        body = r.json()
        assert body["result"] == []
        assert "refused" in body["answer"].lower()

    def test_unsupported_question_handled_gracefully(self):
        with patch("main.llm.generate_sql", return_value="NOT_SUPPORTED"):
            r = client.post("/ask", headers=AUTH_HEADERS, json={"question": "what's the weather?"})

        assert r.status_code == 200
        body = r.json()
        assert body["sql"] is None
        assert body["result"] == []

    def test_llm_unreachable_returns_502_not_500(self):
        with patch("main.llm.generate_sql", side_effect=ConnectionError("network is down")):
            r = client.post("/ask", headers=AUTH_HEADERS, json={"question": "anything"})
        assert r.status_code == 502

    def test_summary_failure_does_not_fail_whole_request(self):
        # if only the second (answer-writing) LLM call fails, the request should
        # still succeed with the real query result, just a fallback answer text
        sql = "SELECT name FROM customers LIMIT 1"
        with patch("main.llm.generate_sql", return_value=sql), \
             patch("main.llm.summarize_result", side_effect=ConnectionError("down")):
            r = client.post("/ask", headers=AUTH_HEADERS, json={"question": "first customer"})

        assert r.status_code == 200
        body = r.json()
        assert len(body["result"]) == 1
        assert "summary" in body["answer"].lower() or "couldn't" in body["answer"].lower()

    def test_health_check(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
