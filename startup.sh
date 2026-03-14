#!/usr/bin/bash
source .venv/bin/activate
uv pip install python-dotenv streamlit openai
uv run streamlit run main.py