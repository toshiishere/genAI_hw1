#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

if [[ ! -d ".venv" ]]; then
  echo "Creating virtual environment..."
  uv venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
uv pip install -U python-dotenv streamlit openai

echo "Starting Streamlit app..."
exec uv run streamlit run main.py
