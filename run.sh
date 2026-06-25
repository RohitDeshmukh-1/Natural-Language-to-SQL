#!/usr/bin/env bash
# Quick setup + run. From the LLM_Rohit/ folder: ./run.sh
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from .env.example - open it and add your real GROQ_API_KEY before this will work."
fi

echo "Installing dependencies..."
pip install -r requirements.txt --quiet

echo "Starting server on http://127.0.0.1:8000 (Ctrl+C to stop)"
cd code
uvicorn main:app --reload --port 8000
