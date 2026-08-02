# Baymax AI Agent

Ask questions about your CSV/Excel data in plain English. Get answers powered by AI.

## Quick Start

```bash
git clone https://github.com/CodeYodha-010/Baymax-Ai-Agent.git
cd Baymax-Ai-Agent/Baymax
pip install -r requirements.txt
```

## Add Your API Key

1. Get a free API key from [openrouter.ai](https://openrouter.ai)
2. Create `.env` file in the `Baymax` folder:

```
OPENROUTER_API_KEY=your_key_here
```

3. Run it:

```bash
python manage.py runserver
```

4. Open `http://127.0.0.1:8000/` in your browser

## Routes

| URL | Method | Description |
|-----|--------|-------------|
| `/` | GET | Home page |
| `/upload/` | GET | Upload form page |
| `/upload/` | POST | Upload CSV/Excel file (multipart/form-data, field: `data_file`) |
| `/upload/ask/` | GET | Ask questions page (requires upload first) |
| `/upload/ask/` | POST | Ask a question (JSON body: `{"query": "..."}`) |

## API Details

### Upload File

```
POST /upload/
Content-Type: multipart/form-data

Body: data_file = <your CSV or Excel file>
```

Response:
```json
{
  "status": "success",
  "message": "Loaded 500 rows, 8 columns.",
  "redirect_url": "/upload/ask/"
}
```

### Ask Question

```
POST /upload/ask/
Content-Type: application/json

Body: {"query": "What is the average sales?"}
```

Response: Server-Sent Events (SSE) stream with these event types:
- `thinking` — processing status
- `code` — generated pandas code
- `result` — final answer

## Example Questions

- "What is the average of column X?"
- "Show top 10 rows"
- "How many rows have status = active?"
- "Find rows containing 'python'"
- "What is the total revenue?"
- "What is the longest text in column name?"
- "Is salary increasing over time?"
- "Are there outliers in the price column?"
- "Give me overview of the dataset"

## Supported Files

- `.csv` — CSV files
- `.xlsx` — Excel files
- `.xls` — Legacy Excel files

Max file size: 100MB
