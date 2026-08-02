# Baymax AI Agent

AI-powered data analysis tool. Upload CSV/Excel files and ask questions in natural language.

## Features

- Natural language queries on your data
- Auto-generates and executes Python/pandas code via LLM
- Streaming responses with thinking process
- CSV and Excel file support (up to 100MB)
- Session-based data persistence
- DuckDB integration for large datasets

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add your OpenRouter API key
python manage.py runserver
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENROUTER_API_KEY` | OpenRouter API key (get one at openrouter.ai) | — |
| `OPENROUTER_MODEL` | LLM model to use | `inclusionai/ling-3.0-flash:free` |
| `SECRET_KEY` | Django secret key | insecure dev key |
| `DEBUG` | Debug mode | `True` |

## Endpoints

| Path | Description |
|------|-------------|
| `/` | Upload CSV/Excel file |
| `/ask/` | Ask questions about uploaded data |

## Stack

- Django 5.2
- Pandas / NumPy / DuckDB
- OpenRouter API (OpenAI-compatible)
