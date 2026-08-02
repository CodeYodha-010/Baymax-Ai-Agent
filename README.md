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

## How It Works

1. Upload a CSV or Excel file
2. Ask a question like "What is the average sales?" or "Show top 10 rows"
3. Get an answer

## Supported Questions

- Lookup: "Find row with id 5"
- Count: "How many rows have status = active?"
- Search: "Find rows containing 'python'"
- Aggregates: "What is the total revenue?"
- Text: "What is the longest sentence in column name?"
- Trends: "Is salary increasing over time?"
- Outliers: "Are there outliers in the price column?"
- Summary: "Give me overview of the dataset"
