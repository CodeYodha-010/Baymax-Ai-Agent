import os
import sys
import io
import json
import time
import re
import logging
import traceback
import datetime
from typing import Dict, List, Any, Generator
from difflib import get_close_matches

import pandas as pd
import numpy as np
from openai import OpenAI

from django.shortcuts import render, redirect
from django.http import JsonResponse, StreamingHttpResponse
from django.conf import settings
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from django.core.files.storage import FileSystemStorage

# --- Configuration ---
MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_EXTENSIONS = ['.csv', '.xlsx', '.xls']
DEFAULT_PANDAS_DISPLAY_ROWS = 100
MAX_RETRIES = 3

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# =============================================================================
# HELPERS
# =============================================================================

class NumpyEncoder(json.JSONEncoder):
    """JSON encoder that handles pandas/numpy/datetime types.

    Used as a safety net in json.dumps(event, cls=NumpyEncoder) so that
    any non-standard type that slips through is serialized correctly.
    """
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date)):
            return obj.isoformat()
        elif isinstance(obj, np.datetime64):
            return str(obj)
        elif isinstance(obj, pd.Timedelta):
            return str(obj)
        elif isinstance(obj, (np.bool_,)):
            return bool(obj)
        try:
            if pd.isna(obj):
                return None
        except (TypeError, ValueError):
            pass
        return super().default(obj)


def sanitize_for_json(obj):
    """Recursively convert any data structure to JSON-safe types.

    This is the DEEP fix — it cleans data at the source before it enters
    the SSE event dict. Handles:
    - pd.Timestamp / datetime → isoformat string
    - np.datetime64 → string
    - pd.Timedelta → string
    - NaN / NaT / None → None (JSON null)
    - np.integer → int, np.floating → float, np.bool_ → bool
    - dict → recursively sanitize values
    - list / tuple → recursively sanitize items
    """
    if obj is None:
        return None
    # Handle pandas/numpy scalar types
    if isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, np.datetime64):
        return str(obj)
    if isinstance(obj, pd.Timedelta):
        return str(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        val = float(obj)
        return None if (val != val) else val  # NaN check
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return [sanitize_for_json(x) for x in obj.tolist()]
    # Handle NaN/NaT (pd.isna returns True for these)
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    # Handle float NaN
    if isinstance(obj, float):
        return None if (obj != obj) else obj
    # Recursively handle containers
    if isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(x) for x in obj]
    # Return everything else as-is (str, int, bool are already JSON-safe)
    return obj


def read_file_robust(file_path: str, file_ext: str) -> pd.DataFrame:
    if file_ext in ['.xlsx', '.xls']:
        return pd.read_excel(file_path)
    encodings = ['utf-8', 'latin1', 'cp1252', 'iso-8859-1']
    for enc in encodings:
        try:
            return pd.read_csv(file_path, encoding=enc, on_bad_lines='skip')
        except Exception:
            continue
    raise ValueError("Could not read file with any supported encoding.")


# =============================================================================
# SAFE CODE EXECUTION
# =============================================================================

def safe_execute_code(code_string: str, df: pd.DataFrame) -> dict:
    logger.info(f"[EXEC] Executing code:\n{code_string}")
    old_stdout = sys.stdout
    try:
        sys.stdout = mystdout = io.StringIO()

        local_vars = {'df': df, 'pd': pd, 'np': np, 'result': None}
        exec(code_string, {}, local_vars)

        sys.stdout = old_stdout
        print_output = mystdout.getvalue()
        result = local_vars.get('result', 'No result variable found')

        # Fallback: if result is string/None, scan for the most recent DataFrame/Series
        # The LLM often forgets to assign result or assigns a string description instead
        if not isinstance(result, (pd.DataFrame, pd.Series)):
            last_chartable = None
            for key, val in local_vars.items():
                if key in ('df', 'pd', 'np', 'result'):
                    continue
                if isinstance(val, (pd.DataFrame, pd.Series)):
                    last_chartable = val
            if last_chartable is not None:
                logger.info(f"[EXEC] Fallback: result was {type(result).__name__}, found {type(last_chartable).__name__} in local vars")
                result = last_chartable

        if isinstance(result, (pd.DataFrame, pd.Series)):
            if isinstance(result, pd.DataFrame):
                result_str = f"DataFrame Shape: {result.shape}\n"
                result_str += result.head(DEFAULT_PANDAS_DISPLAY_ROWS).to_string()
                if len(result) > 10:
                    result_str += "\n\nSummary Stats:\n" + result.describe().to_string()
            else:
                result_str = result.to_string()
        else:
            result_str = str(result)

        final_output = ""
        if print_output:
            final_output += f"Console Output:\n{print_output}\n"
        final_output += f"Result Variable:\n{result_str}"

        logger.info(f"[EXEC] Success. Output length: {len(final_output)} chars. Result type: {type(result).__name__}")
        return {"status": "success", "output": final_output, "result": result}

    except Exception as e:
        if 'old_stdout' in dir():
            sys.stdout = old_stdout
        else:
            sys.stdout = sys.__stdout__
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"[EXEC] Failed: {error_msg}")
        return {"status": "error", "error": error_msg}


# =============================================================================
# DATA CLEANING & SCHEMA PROFILING
# =============================================================================

class DataCleaner:
    def __init__(self, df: pd.DataFrame):
        self.df = df.copy()

    def clean(self) -> pd.DataFrame:
        self.df.columns = (
            self.df.columns.str.lower()
            .str.replace(r'[^\w\s]', '', regex=True)
            .str.replace(r'\s+', '_', regex=True)
        )
        return self.df


def generate_rich_schema(df: pd.DataFrame) -> dict:
    schema = {"rows": len(df), "columns": []}

    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    correlations = {}
    if len(numeric_cols) > 1:
        corr_matrix = df[numeric_cols].corr()
        for i, col1 in enumerate(numeric_cols):
            for col2 in numeric_cols[i + 1:]:
                correlations[f"{col1}_vs_{col2}"] = float(corr_matrix.loc[col1, col2])

    null_counts = df.isnull().sum().to_dict()
    duplicate_rows = int(df.duplicated().sum())

    for col in df.columns:
        col_info = {"name": col, "type": str(df[col].dtype)}
        col_info["null_count"] = int(null_counts[col])
        col_info["null_percentage"] = float(null_counts[col] / len(df) * 100)

        if pd.api.types.is_numeric_dtype(df[col]):
            col_info["min"] = float(df[col].min()) if not df[col].empty else None
            col_info["max"] = float(df[col].max()) if not df[col].empty else None
            col_info["mean"] = float(df[col].mean()) if not df[col].empty else None
            col_info["std"] = float(df[col].std()) if not df[col].empty else None
            col_info["classification"] = "metric"
        elif pd.api.types.is_datetime64_any_dtype(df[col]):
            col_info["min_date"] = df[col].min().isoformat() if not df[col].empty else None
            col_info["max_date"] = df[col].max().isoformat() if not df[col].empty else None
            col_info["classification"] = "temporal"
        elif df[col].dtype == 'object':
            unique_count = df[col].nunique()
            col_info["unique_count"] = unique_count
            col_info["unique_percentage"] = float(unique_count / len(df) * 100)
            col_info["classification"] = "categorical" if unique_count / len(df) < 0.05 else "dimension"
            if unique_count <= 50:
                col_info["examples"] = df[col].dropna().unique().tolist()[:10]
            else:
                col_info["examples"] = ["(hidden - many unique values)"]
        else:
            col_info["classification"] = "other"

        schema["columns"].append(col_info)

    schema["duplicate_rows"] = duplicate_rows
    schema["duplicate_percentage"] = float(duplicate_rows / len(df) * 100)
    schema["correlations"] = correlations
    return schema


# =============================================================================
# ERROR PARSING & AUTO-CORRECTION
# =============================================================================

def auto_correct_code(code: str, error_info: dict, columns: List[str]) -> str:
    corrected = code
    if error_info.get("type") == "column_not_found" and error_info.get("column"):
        bad_col = error_info["column"]
        matches = get_close_matches(bad_col.lower(), [c.lower() for c in columns], n=1, cutoff=0.6)
        if matches:
            real_col = [c for c in columns if c.lower() == matches[0]][0]
            corrected = corrected.replace(bad_col, real_col)
    elif error_info.get("type") == "series_orient":
        corrected = re.sub(
            r'\.to_dict\(\s*orient\s*=\s*["\'][^"\']*["\']\s*\)',
            '.to_dict()',
            corrected
        )
    return corrected


def parse_error(error_msg: str) -> dict:
    info = {"type": "unknown", "column": None, "details": error_msg}
    col_match = re.search(r"Column '([^']+)' not found", error_msg)
    if col_match:
        info["type"] = "column_not_found"
        info["column"] = col_match.group(1)
    elif "to_dict() got an unexpected keyword argument" in error_msg:
        info["type"] = "series_orient"
    elif "does not exist" in error_msg.lower():
        col_match2 = re.search(r"'([^']+)'", error_msg)
        if col_match2:
            info["type"] = "column_not_found"
            info["column"] = col_match2.group(1)
    return info


def extract_code_from_response(text: str) -> str:
    """Extract Python code from LLM response. Handles markdown code blocks and raw JSON."""
    # Try to find code in markdown code blocks
    code_match = re.search(r'```(?:python)?\s*\n(.*?)```', text, re.DOTALL)
    if code_match:
        return code_match.group(1).strip()

    # Try to parse as JSON with "code" field
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "code" in data:
            return data["code"]
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find JSON-like structure with code field
    json_match = re.search(r'\{[^{}]*"code"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if json_match:
        try:
            code = json_match.group(1).encode().decode('unicode_escape')
            return code
        except Exception:
            pass

    # Last resort: return the text as-is (might be raw code)
    return text.strip()


def extract_thinking_from_response(text: str) -> str:
    """Extract thinking/explanation from LLM response."""
    # Try JSON "thinking" field
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "thinking" in data:
            return data["thinking"]
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find JSON-like structure with thinking field
    json_match = re.search(r'\{[^{}]*"thinking"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if json_match:
        try:
            return json_match.group(1).encode().decode('unicode_escape')
        except Exception:
            pass

    # Remove code blocks and return remaining text as thinking
    cleaned = re.sub(r'```.*?```', '', text, flags=re.DOTALL).strip()
    return cleaned if cleaned else ""


# =============================================================================
# CHART DATA EXTRACTION
# =============================================================================

MAX_CHART_POINTS = 50

CHART_COLORS = [
    '#4e73df', '#1cc88a', '#36b9cc', '#f6c23e',
    '#e74a3b', '#858796', '#5a5c69', '#2e59d9',
]


def extract_chart_data(result, suggested_type: str = None) -> dict:
    """Extract chart-ready data from a pandas result (Series or DataFrame).

    Returns dict with chartType, title, labels, datasets — or None if not chartable.
    Handles Series, multi-column DataFrames, single-column DataFrames, and numeric-only DataFrames.
    All output is sanitized via sanitize_for_json() to ensure JSON-safe types.
    """
    # --- Series handling ---
    if isinstance(result, pd.Series):
        if len(result) == 0:
            return None
        # Skip Series with all non-numeric (string) data
        if result.dtype == 'object':
            return None
        labels = [str(x) for x in result.index.tolist()[:MAX_CHART_POINTS]]
        values = [float(x) if pd.notna(x) else 0 for x in result.values[:MAX_CHART_POINTS]]
        chart_type = suggested_type or 'bar'
        return sanitize_for_json({
            'chartType': chart_type,
            'title': '',
            'labels': labels,
            'datasets': [{'label': str(result.name) if result.name else 'Value', 'data': values}],
        })

    # --- DataFrame handling ---
    if isinstance(result, pd.DataFrame):
        if result.empty:
            return None

        numeric_cols = result.select_dtypes(include=[np.number]).columns.tolist()
        non_numeric_cols = result.select_dtypes(exclude=[np.number]).columns.tolist()
        df = result.head(MAX_CHART_POINTS)

        # Case 0: OHLC (candlestick) detection — Open, High, Low, Close columns
        col_names_lower = [str(c).lower() for c in result.columns]
        has_ohlc = all(col in col_names_lower for col in ['open', 'high', 'low', 'close'])
        if has_ohlc:
            # Find the actual column names (case-insensitive)
            col_map = {str(c).lower(): c for c in result.columns}
            o_col, h_col, l_col, c_col = col_map['open'], col_map['high'], col_map['low'], col_map['close']
            # Find label column (first non-numeric column, or use index)
            label_col = None
            for c in result.columns:
                if str(c).lower() not in ['open', 'high', 'low', 'close', 'volume', 'trend', 'change_%']:
                    if not pd.api.types.is_numeric_dtype(result[c]):
                        label_col = c
                        break
            if label_col:
                labels = [str(x) for x in df[label_col].tolist()]
            else:
                labels = [str(idx) for idx in df.index.tolist()]
            # Build candlestick data: [open, high, low, close] per point
            ohlc_data = []
            for _, row in df.iterrows():
                ohlc_data.append([
                    float(row[o_col]) if pd.notna(row[o_col]) else 0,
                    float(row[h_col]) if pd.notna(row[h_col]) else 0,
                    float(row[l_col]) if pd.notna(row[l_col]) else 0,
                    float(row[c_col]) if pd.notna(row[c_col]) else 0,
                ])
            return sanitize_for_json({
                'chartType': 'candlestick',
                'title': 'OHLC Chart',
                'labels': labels,
                'datasets': [{'label': 'OHLC', 'data': ohlc_data}],
            })

        # Case 1: Has both non-numeric (labels) and numeric (values) columns
        if non_numeric_cols and numeric_cols:
            labels = [str(x) for x in df[non_numeric_cols[0]].tolist()]
            datasets = []
            for col in numeric_cols[:4]:
                datasets.append({
                    'label': str(col),
                    'data': [float(x) if pd.notna(x) else 0 for x in df[col].tolist()],
                })
            chart_type = suggested_type or ('pie' if len(df) <= 8 and len(numeric_cols) == 1 else 'bar')
            return sanitize_for_json({
                'chartType': chart_type,
                'title': '',
                'labels': labels,
                'datasets': datasets,
            })

        # Case 2: All numeric columns — use row index as labels
        if numeric_cols and not non_numeric_cols:
            labels = [str(idx) for idx in df.index.tolist()[:MAX_CHART_POINTS]]
            datasets = []
            for col in numeric_cols[:4]:
                datasets.append({
                    'label': str(col),
                    'data': [float(x) if pd.notna(x) else 0 for x in df[col].tolist()],
                })
            chart_type = suggested_type or ('bar' if len(numeric_cols) > 1 else 'line')
            return sanitize_for_json({
                'chartType': chart_type,
                'title': '',
                'labels': labels,
                'datasets': datasets,
            })

        # Case 3: Single numeric column with non-numeric index (common from groupby)
        if len(numeric_cols) == 1 and len(non_numeric_cols) == 0:
            labels = [str(idx) for idx in df.index.tolist()[:MAX_CHART_POINTS]]
            values = [float(x) if pd.notna(x) else 0 for x in df[numeric_cols[0]].tolist()[:MAX_CHART_POINTS]]
            chart_type = suggested_type or 'bar'
            return sanitize_for_json({
                'chartType': chart_type,
                'title': '',
                'labels': labels,
                'datasets': [{'label': str(numeric_cols[0]), 'data': values}],
            })

        # Case 4: Two numeric columns — treat first as X, second as Y (scatter)
        if len(numeric_cols) >= 2:
            x_col = numeric_cols[0]
            y_col = numeric_cols[1]
            labels = [str(x) for x in df[x_col].tolist()]
            chart_type = suggested_type or 'scatter'
            return sanitize_for_json({
                'chartType': chart_type,
                'title': '',
                'labels': labels,
                'datasets': [{
                    'label': str(y_col),
                    'data': [float(x) if pd.notna(x) else 0 for x in df[y_col].tolist()],
                }],
            })

        # Case 5: Single-column DataFrame — use index as labels, column as values
        if len(result.columns) == 1:
            col = result.columns[0]
            if pd.api.types.is_numeric_dtype(result[col]):
                labels = [str(idx) for idx in df.index.tolist()[:MAX_CHART_POINTS]]
                values = [float(x) if pd.notna(x) else 0 for x in df[col].tolist()[:MAX_CHART_POINTS]]
                chart_type = suggested_type or 'bar'
                return sanitize_for_json({
                    'chartType': chart_type,
                    'title': '',
                    'labels': labels,
                    'datasets': [{'label': str(col), 'data': values}],
                })

    return None


def extract_df_payload(result) -> dict:
    """Extract tabular data payload from a DataFrame result.

    Returns dict with columns, rows, row_count — or None if not a DataFrame.
    All values are sanitized via sanitize_for_json() to ensure JSON-safe types.
    """
    if not isinstance(result, pd.DataFrame) or result.empty:
        return None
    df = result.head(MAX_CHART_POINTS)
    return sanitize_for_json({
        'columns': [str(c) for c in df.columns.tolist()],
        'rows': df.values.tolist(),
        'row_count': len(result),
    })


# =============================================================================
# LLM CLIENT (Simplified, single-shot)
# =============================================================================

class LLMClient:
    """Simplified LLM client. One call for code generation, one for humanization."""

    def __init__(self):
        self.client = OpenAI(
            base_url=getattr(settings, 'OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1'),
            api_key=getattr(settings, 'OPENROUTER_API_KEY', ''),
            timeout=120.0,
            max_retries=2
        )
        self.model = getattr(settings, 'OPENROUTER_MODEL', 'inclusionai/ling-3.0-flash:free')

        if not self.client.api_key:
            logger.warning("API key is not configured!")
        else:
            logger.info(f"[LLMClient] Initialized | Model: {self.model}")

    def _is_rate_limit_error(self, error: Exception) -> bool:
        error_str = str(error).lower()
        return any(keyword in error_str for keyword in [
            'resourceexhausted', 'resource_exhausted', 'resource exhausted',
            'rate limit', 'too many requests', '429', 'capacity', 'throttl'
        ])

    def _call(self, messages: list, max_tokens: int = 4096) -> dict:
        """Non-streaming LLM call with retry on rate limits."""
        for attempt in range(3):
            try:
                completion = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=max_tokens,
                    stream=False,
                )
                return completion.model_dump()
            except Exception as e:
                if self._is_rate_limit_error(e) and attempt < 2:
                    wait = (2 ** attempt) * 5
                    logger.warning(f"Rate limited, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"LLM call error: {e}")
                return {"error": str(e)}
        return {"error": "Failed after retries"}

    def _call_streaming(self, messages: list, max_tokens: int = 4096):
        """Streaming LLM call. Yields content chunks."""
        for attempt in range(3):
            try:
                completion = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=max_tokens,
                    stream=True,
                )
                full_content = ""
                full_reasoning = ""
                for chunk in completion:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        full_reasoning += reasoning
                        yield {"type": "reasoning", "content": reasoning}
                    if delta.content is not None:
                        full_content += delta.content
                        yield {"type": "content", "content": delta.content}
                yield {"type": "done", "content": full_content, "reasoning": full_reasoning}
                return
            except Exception as e:
                if self._is_rate_limit_error(e) and attempt < 2:
                    wait = (2 ** attempt) * 5
                    logger.warning(f"Rate limited, retrying in {wait}s...")
                    yield {"type": "thinking", "content": f"\n\n[Rate limited, retrying in {wait}s...]"}
                    time.sleep(wait)
                    continue
                logger.error(f"LLM streaming error: {e}")
                yield {"type": "error", "message": str(e)}
                return

    def generate_code(self, schema: dict, query: str, sample_data: str, error_context: str = "") -> dict:
        """Single LLM call that generates pandas code to answer the user's question."""
        schema_str = json.dumps(schema, indent=2, cls=NumpyEncoder)

        system_prompt = f"""You are an expert Python Data Scientist. Write pandas code to answer the user's question about their dataset.

Dataset Schema:
{schema_str}

Sample Data (first 10 rows):
{sample_data}

IMPORTANT — UNDERSTAND THE QUERY FIRST:
- If the user is greeting you (hi, hello, hey, good morning, etc.), respond with a SHORT friendly greeting. Do NOT write any code. Example: result = "Hey! I'm Baymax, your data analyst. Ask me anything about your data!"
- If the user asks something unrelated to data analysis (jokes, general knowledge, opinions, etc.), respond with a SHORT polite message redirecting them to data questions. Example: result = "I'm here to help you analyze your data! Try asking something like 'What is the average of column X?'"
- If the question IS about data analysis, proceed to write pandas code below.

ONLY write pandas code when the question is clearly about analyzing, querying, summarizing, or visualizing the uploaded dataset.

CRITICAL RULES FOR DATA QUESTIONS:
- Use pandas (pd) and numpy (np) — they are already imported
- ALWAYS assign your final answer to a variable named `result`
- The `result` must be INFORMATIVE — never return just a single name or number alone
- Always include relevant numbers, comparisons, and context in your result
  - For "highest/lowest" questions: show the value AND the number (e.g. "Desktop: $1,250 discount")
  - For aggregations: show a summary with key numbers
  - For comparisons: show a ranked list with values
- Handle null values with .isna() / .dropna()
- Verify column names match the schema exactly
- Wrap operations in try/except for safety

CHARTING RULES (VERY IMPORTANT):
- NEVER use matplotlib, seaborn, plotly, plt.plot, plt.savefig, or ANY Python plotting library
- NEVER save charts to files (no .png, .jpg, .svg, etc.)
- The frontend has its own charting system — it will automatically render interactive charts from your data
- When the user asks to visualize, chart, plot, graph, show trends, compare, break down, or analyze distributions:
  → Return a pandas Series or DataFrame as `result` (NOT a string)
  → For single-column comparisons: use a Series (e.g. result = df.groupby('category')['revenue'].sum().sort_values(ascending=False))
  → For multi-column comparisons: use a DataFrame with meaningful column names
  → For time trends: use a Series with the time column as index
  → For distributions: use value_counts() or a summary DataFrame
  → Keep the data clean and well-structured — the chart system will handle the rest
- When the question is a simple factual answer (not visual), return a descriptive string as `result`

Example of GOOD result (string for factual questions):
  result = "Desktop ($1,250) has the highest discount, followed by Laptop ($890) and Phone ($450)"

Example of GOOD result (Series for charting):
  result = df.groupby('category')['revenue'].sum().sort_values(ascending=False)

Example of GOOD result (DataFrame for multi-series charting):
  result = df.groupby('category')[['revenue', 'cost']].sum()

Example of GOOD result (greeting — NO code needed):
  result = "Hey! I'm Baymax. Ask me anything about your data — like 'What's the total revenue?' or 'Show me top 10 rows'"

Example of BAD result:
  result = "Desktop"
  (this gives no context — avoid this!)

Example of BAD result (matplotlib — NEVER do this):
  import matplotlib.pyplot as plt
  df.plot(kind='bar')
  plt.savefig('chart.png')
  result = "Chart saved"
  (NEVER use matplotlib — return the DataFrame instead)"""

        user_message = f"Question: {query}"

        if error_context:
            user_message += f"""

The previous code failed with this error:
{error_context}

Fix the error and generate corrected code."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]

        logger.info(f"[LLM] Generating code for: {query[:80]}")
        response = self._call(messages, max_tokens=4096)

        if "error" in response:
            return {"error": response["error"]}

        content = response["choices"][0]["message"]["content"]
        code = extract_code_from_response(content)
        thinking = extract_thinking_from_response(content)

        logger.info(f"[LLM] Generated code ({len(code)} chars)")
        return {"code": code, "thinking": thinking}

    def humanize_result(self, query: str, raw_output: str) -> str:
        """Convert raw code output into a friendly, easy-to-understand answer."""
        truncated = raw_output[:6000]
        messages = [
            {"role": "system", "content": (
                "You are a friendly data analyst assistant. "
                "Convert the raw data output below into a clear, easy-to-understand answer. "
                "Write like you are explaining to a curious friend who is not technical. "
                "Use simple sentences. Include actual numbers and values from the data. "
                "Use Markdown formatting to structure your answer:\n"
                "- Use **bold** for key numbers and important findings\n"
                "- Use bullet lists for breakdowns and comparisons\n"
                "- Use Markdown tables for tabular data (| col1 | col2 | format)\n"
                "- Use `backticks` for column names and code references\n"
                "- Use ### headers to separate sections if the answer is long\n"
                "- Keep paragraphs short (2-3 sentences max)\n"
                "Answer the question directly first, then share interesting patterns or insights. "
                "Be thorough but natural — like a helpful conversation."
            )},
            {"role": "user", "content": f"User asked: {query}\n\nRaw data output:\n{truncated}"}
        ]
        logger.info(f"[HUMANIZE] Generating friendly answer for: {query[:80]}")
        response = self._call(messages, max_tokens=2000)
        if "error" in response:
            logger.warning(f"[HUMANIZE] LLM call failed: {response['error']}")
            return raw_output[:2000]
        if "choices" in response:
            content = response["choices"][0]["message"]["content"]
            if not content:
                logger.warning("[HUMANIZE] LLM returned empty content, using raw output")
                return raw_output[:2000]
            logger.info(f"[HUMANIZE] Result length: {len(content)} chars")
            return content
        logger.warning("[HUMANIZE] LLM call failed, returning raw output")
        return raw_output[:2000]


# =============================================================================
# MATPLOTLIB STRIPPING (defensive — don't trust the LLM)
# =============================================================================

def strip_matplotlib(code: str) -> str:
    """Remove matplotlib/seaborn/plotly code from generated code.

    Even if the LLM ignores the prompt rules and generates plotting code,
    this transforms .plot() calls into 'result = <data_expression>' so the
    chart data is preserved and can be rendered by Highcharts.
    """
    lines = code.split('\n')
    cleaned = []
    result_assigned = False

    for line in lines:
        stripped = line.strip()

        # Skip import lines for plotting libraries
        if any(lib in stripped for lib in [
            'import matplotlib', 'from matplotlib',
            'import seaborn', 'from seaborn',
            'import plotly', 'from plotly',
            'import plt', 'plt ='
        ]):
            continue

        # Skip plt.* calls (plt.show, plt.savefig, plt.title, etc.)
        if stripped.startswith('plt.') or 'plt.' in stripped:
            continue

        # Transform .plot()/.hist()/.boxplot() calls into result assignments
        # This preserves the data so the frontend can render it as a chart
        match = re.match(r'^(.+?)\s*\.(plot|hist|boxplot)\s*\(', stripped)
        if match:
            data_expr = match.group(1)
            # Don't transform if it's a plt.plot() call (already skipped above)
            if not data_expr.startswith('plt'):
                # Only assign to result if not already assigned
                if not result_assigned:
                    cleaned.append(f'result = {data_expr}')
                    result_assigned = True
                continue

        # Remove string result assignments (e.g. result = "Chart created")
        # These overwrite the transformed data assignment and kill the chart
        if re.match(r'^result\s*=\s*["\']', stripped):
            continue

        # Skip savefig calls
        if 'savefig' in stripped:
            continue

        # Skip show() calls
        if stripped.endswith('.show()') or stripped == 'plt.show()':
            continue

        cleaned.append(line)

    result = '\n'.join(cleaned)
    if result != code:
        logger.info("[STRIP] Transformed plotting code to result assignment")
    return result


# =============================================================================
# DATA ANALYSIS AGENT (Single-shot + retry)
# =============================================================================

class DataAnalysisAgent:
    """Simplified agent: generate code → execute → retry on error → return result.

    Flow:
    1. One LLM call generates pandas code
    2. Code is executed
    3. If error → auto-correct or feed error back to LLM (max 3 attempts)
    4. If success → humanize result into friendly answer
    """

    def __init__(self, client: LLMClient, df: pd.DataFrame):
        self.client = client
        self.df = df

    def _build_sample_data(self) -> str:
        """Build a string of sample data for the LLM prompt."""
        head = self.df.head(10)
        dtypes = self.df.dtypes
        sample = f"Columns: {list(self.df.columns)}\n"
        sample += f"Dtypes:\n{dtypes.to_string()}\n\n"
        sample += f"First 10 rows:\n{head.to_string()}"
        return sample

    def analyze(self, query: str, schema: dict) -> Generator[dict, None, None]:
        """Yield SSE events. Single-shot code generation with retry."""
        logger.info(f"[AGENT] Query: '{query}'")
        yield {"type": "thinking", "content": "Analyzing your question..."}

        sample_data = self._build_sample_data()
        error_context = ""

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"[AGENT] Attempt {attempt}/{MAX_RETRIES}")

            # Step 1: Generate code (1 LLM call)
            yield {"type": "thinking", "content": f"\nGenerating analysis code (attempt {attempt})..."}
            result = self.client.generate_code(schema, query, sample_data, str(error_context))

            if "error" in result:
                yield {"type": "error", "message": f"AI Error: {result['error']}"}
                return

            code = result["code"]
            # Strip any matplotlib/seaborn code that the LLM might have generated
            code = strip_matplotlib(code)
            thinking = result.get("thinking", "")

            if thinking:
                yield {"type": "step_log", "content": thinking}

            yield {"type": "code", "content": code}
            yield {"type": "step_log", "content": "Executing analysis..."}

            # Step 2: Execute code
            exec_result = safe_execute_code(code, self.df)

            if exec_result["status"] == "success":
                raw_output = exec_result["output"]
                result_value = exec_result.get("result")
                logger.info(f"[AGENT] Code executed successfully ({len(raw_output)} chars). Result type: {type(result_value).__name__}")

                # B1: Emit chart event if result is chartable
                chart_data = extract_chart_data(result_value)
                if chart_data:
                    yield {"type": "chart", "chart": chart_data}
                else:
                    logger.info(f"[AGENT] No chart generated. Result type: {type(result_value).__name__}")

                # B2: Emit data event if result is a DataFrame
                df_payload = extract_df_payload(result_value)
                if df_payload:
                    yield {"type": "data", "payload": df_payload}

                yield {"type": "step_log", "content": "Analysis complete. Generating answer..."}
                try:
                    human_answer = self.client.humanize_result(query, raw_output)
                except Exception as e:
                    logger.error(f"[AGENT] Humanize failed: {e}")
                    human_answer = raw_output[:2000]
                yield {"type": "result", "answer": human_answer, "raw_data": raw_output[:2000], "status": "success"}
                return

            # Step 4: Code failed — try auto-correction first
            error_msg = exec_result["error"]
            logger.warning(f"[AGENT] Code error: {error_msg}")
            yield {"type": "step_log", "content": f"Code error: {error_msg}"}

            error_info = parse_error(error_msg)
            corrected_code = auto_correct_code(code, error_info, self.df.columns.tolist())

            if corrected_code != code:
                logger.info("[AGENT] Auto-corrected code, retrying execution...")
                exec_result = safe_execute_code(corrected_code, self.df)
                if exec_result["status"] == "success":
                    raw_output = exec_result["output"]
                    result_value = exec_result.get("result")

                    chart_data = extract_chart_data(result_value)
                    if chart_data:
                        yield {"type": "chart", "chart": chart_data}
                    else:
                        logger.info(f"[AGENT] No chart generated (auto-corrected). Result type: {type(result_value).__name__}")

                    df_payload = extract_df_payload(result_value)
                    if df_payload:
                        yield {"type": "data", "payload": df_payload}

                    yield {"type": "step_log", "content": "Auto-correction succeeded!"}
                    try:
                        human_answer = self.client.humanize_result(query, raw_output)
                    except Exception as e:
                        logger.error(f"[AGENT] Humanize failed (auto-corrected): {e}")
                        human_answer = raw_output[:2000]
                    yield {"type": "result", "answer": human_answer, "raw_data": raw_output[:2000], "status": "success"}
                    return

            # Auto-correction didn't work — feed error to LLM on next attempt
            error_context = f"Previous code:\n```python\n{code}\n```\n\nError: {error_msg}"
            yield {"type": "step_log", "content": f"Retrying with error context..."}

        # All retries exhausted
        logger.error(f"[AGENT] All {MAX_RETRIES} attempts failed")
        yield {"type": "error", "message": "Could not generate working code after multiple attempts. Try rephrasing your question."}


# =============================================================================
# DJANGO VIEWS
# =============================================================================

@method_decorator(csrf_exempt, name='dispatch')
class UploadFileView(View):
    template_name = 'upload.html'
    SESSION_FILE_KEY = 'uploaded_file_path'
    SESSION_SCHEMA_KEY = 'table_schema'

    def get(self, request):
        return render(request, self.template_name, {
            'form_title': 'Upload Data',
            'max_file_size': MAX_FILE_SIZE
        })

    def post(self, request):
        try:
            if 'data_file' not in request.FILES:
                return JsonResponse({'status': 'error', 'message': 'No file provided.'})

            file = request.FILES['data_file']
            file_ext = os.path.splitext(file.name)[1].lower()
            logger.info(f"[UPLOAD] File: {file.name} | Size: {file.size} bytes | Type: {file_ext}")

            if file_ext not in ALLOWED_EXTENSIONS:
                return JsonResponse({'status': 'error', 'message': 'Invalid file type. Use CSV or Excel.'})
            if file.size > MAX_FILE_SIZE:
                return JsonResponse({'status': 'error', 'message': f'File too large (max {MAX_FILE_SIZE_MB}MB).'})

            fs = FileSystemStorage()
            filename = fs.save(file.name, file)
            file_path = fs.path(filename)

            try:
                df = read_file_robust(file_path, file_ext)
            except ValueError as e:
                return JsonResponse({'status': 'error', 'message': str(e)})

            logger.info(f"[UPLOAD] Loaded DataFrame: {df.shape[0]} rows x {df.shape[1]} columns")
            logger.info(f"[UPLOAD] Columns: {list(df.columns)}")
            logger.info(f"[UPLOAD] Dtypes:\n{df.dtypes.to_string()}")

            cleaner = DataCleaner(df)
            cleaned_df = cleaner.clean()

            if file_ext == '.csv':
                cleaned_df.to_csv(file_path, index=False)
            else:
                cleaned_df.to_excel(file_path, index=False)

            schema = generate_rich_schema(cleaned_df)
            request.session[self.SESSION_FILE_KEY] = file_path
            request.session[self.SESSION_SCHEMA_KEY] = schema
            request.session['original_filename'] = file.name

            logger.info(f"[UPLOAD] Schema generated: {len(schema['columns'])} columns profiled")
            logger.info(f"[UPLOAD] Duplicate rows: {schema.get('duplicate_rows', 0)}")
            return JsonResponse({
                'status': 'success',
                'message': f'Loaded {len(cleaned_df)} rows, {len(cleaned_df.columns)} columns.',
                'redirect_url': '/upload/ask/'
            })

        except Exception as e:
            logger.error(f"[UPLOAD] Error: {traceback.format_exc()}")
            return JsonResponse({'status': 'error', 'message': str(e)})


@method_decorator(csrf_exempt, name='dispatch')
class AskQuestionView(View):
    template_name = 'ask.html'
    SESSION_FILE_KEY = 'uploaded_file_path'
    SESSION_SCHEMA_KEY = 'table_schema'

    def get(self, request):
        file_path = request.session.get(self.SESSION_FILE_KEY)
        schema = request.session.get(self.SESSION_SCHEMA_KEY)
        if not file_path:
            messages.warning(request, 'Please upload a file first.')
            return redirect('upload')

        context = {
            'file_name': request.session.get('original_filename', 'Data'),
            'row_count': schema.get('rows', 0) if schema else 0,
            'columns': [c['name'] for c in schema.get('columns', [])] if schema else [],
            'sample_data': []
        }
        return render(request, self.template_name, context)

    def post(self, request):
        try:
            data = json.loads(request.body)
            query = data.get('query', '').strip()
            if not query:
                return JsonResponse({'status': 'error', 'message': 'No question provided.'})

            file_path = request.session.get(self.SESSION_FILE_KEY)
            schema = request.session.get(self.SESSION_SCHEMA_KEY)
            logger.info(f"[ASK] Query: '{query}'")
            logger.info(f"[ASK] File path: {file_path}")

            if not file_path or not os.path.exists(file_path):
                logger.error(f"[ASK] Session expired or file missing: {file_path}")
                return JsonResponse({'status': 'error', 'message': 'Session expired. Upload again.'})

            if file_path.endswith('.csv'):
                df = pd.read_csv(file_path)
            else:
                df = pd.read_excel(file_path)

            logger.info(f"[ASK] Loaded DataFrame: {df.shape[0]} rows x {df.shape[1]} columns")

            client = LLMClient()
            agent = DataAnalysisAgent(client, df)

            def event_stream():
                for event in agent.analyze(query, schema):
                    logger.debug(f"[ASK] SSE event: {event['type']}")
                    yield f"data: {json.dumps(event, cls=NumpyEncoder)}\n\n"
                logger.info(f"[ASK] Stream complete for query: '{query}'")

            return StreamingHttpResponse(
                event_stream(),
                content_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                }
            )

        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid request format.'})
        except Exception as e:
            logger.error(f"[ASK] Error: {traceback.format_exc()}")
            return JsonResponse({'status': 'error', 'message': str(e)})


@method_decorator(csrf_exempt, name='dispatch')
class VisualizeView(View):
    template_name = 'visualize.html'
    SESSION_FILE_KEY = 'uploaded_file_path'
    SESSION_SCHEMA_KEY = 'table_schema'

    def get(self, request):
        file_path = request.session.get(self.SESSION_FILE_KEY)
        schema = request.session.get(self.SESSION_SCHEMA_KEY)
        if not file_path:
            messages.warning(request, 'Please upload a file first.')
            return redirect('upload')

        columns = [col['name'] for col in schema.get('columns', [])]

        context = {
            'file_name': request.session.get('original_filename', 'Data'),
            'row_count': schema.get('rows', 0) if schema else 0,
            'columns': columns,
        }
        return render(request, self.template_name, context)

    def post(self, request):
        try:
            data = json.loads(request.body)
            x_col = data.get('x_col', '').strip()
            y_col = data.get('y_col', '').strip()
            chart_type = data.get('chart_type', 'bar').strip()
            agg_func = data.get('agg_func', 'none').strip()

            if not x_col:
                return JsonResponse({'status': 'error', 'message': 'X-axis column is required.'})

            file_path = request.session.get(self.SESSION_FILE_KEY)
            if not file_path or not os.path.exists(file_path):
                return JsonResponse({'status': 'error', 'message': 'Session expired. Upload again.'})

            if file_path.endswith('.csv'):
                df = pd.read_csv(file_path)
            else:
                df = pd.read_excel(file_path)

            if x_col not in df.columns:
                return JsonResponse({'status': 'error', 'message': f"Column '{x_col}' not found."})
            if y_col and y_col not in df.columns:
                return JsonResponse({'status': 'error', 'message': f"Column '{y_col}' not found."})

            # Validate Y-axis column is numeric (required for most chart types)
            if y_col and chart_type != 'histogram':
                if not pd.api.types.is_numeric_dtype(df[y_col]):
                    return JsonResponse({
                        'status': 'error',
                        'message': f"Y-axis column '{y_col}' contains text, not numbers. Select a numeric column for the Y-axis, or remove the Y-axis selection to use count instead."
                    })
            # Validate X-axis column is numeric for scatter
            if chart_type == 'scatter' and x_col:
                if not pd.api.types.is_numeric_dtype(df[x_col]):
                    return JsonResponse({
                        'status': 'error',
                        'message': f"X-axis column '{x_col}' must be numeric for scatter chart. Try a bar chart instead."
                    })

            # Build chart data based on chart type
            chart_result = None

            if chart_type == 'histogram':
                col_data = df[x_col].dropna()
                if not pd.api.types.is_numeric_dtype(col_data):
                    return JsonResponse({'status': 'error', 'message': f"Column '{x_col}' must be numeric for histogram."})
                counts, bin_edges = np.histogram(col_data, bins=20)
                labels = [f"{bin_edges[i]:.1f}-{bin_edges[i+1]:.1f}" for i in range(len(counts))]
                chart_result = {
                    'chartType': 'column',
                    'title': f'Distribution of {x_col}',
                    'labels': labels,
                    'datasets': [{'label': 'Count', 'data': counts.tolist()}],
                }

            elif chart_type == 'pie':
                if y_col:
                    grouped = df.groupby(x_col)[y_col].sum()
                else:
                    grouped = df[x_col].value_counts()
                chart_result = {
                    'chartType': 'pie',
                    'title': f'{y_col or "Count"} by {x_col}',
                    'labels': [str(x) for x in grouped.index.tolist()],
                    'datasets': [{'label': y_col or 'Count', 'data': [float(v) for v in grouped.values.tolist()]}],
                }

            elif chart_type == 'scatter':
                if not y_col:
                    return JsonResponse({'status': 'error', 'message': 'Scatter chart requires a Y-axis column.'})
                sample = df[[x_col, y_col]].dropna().head(MAX_CHART_POINTS)
                chart_result = {
                    'chartType': 'scatter',
                    'title': f'{y_col} vs {x_col}',
                    'labels': [str(x) for x in sample[x_col].tolist()],
                    'datasets': [{'label': y_col, 'data': [{'x': float(r[x_col]), 'y': float(r[y_col])} for _, r in sample.iterrows()]}],
                }

            elif chart_type == 'line' or chart_type == 'area':
                if y_col:
                    if agg_func and agg_func != 'none':
                        grouped = df.groupby(x_col)[y_col].agg(agg_func)
                    else:
                        grouped = df.groupby(x_col)[y_col].first()
                else:
                    grouped = df[x_col].value_counts().sort_index()
                chart_result = {
                    'chartType': chart_type,
                    'title': f'{y_col or "Count"} by {x_col}',
                    'labels': [str(x) for x in grouped.index.tolist()],
                    'datasets': [{'label': y_col or 'Count', 'data': [float(v) if pd.notna(v) else 0 for v in grouped.values.tolist()]}],
                }

            else:
                # bar (default)
                if y_col:
                    if agg_func and agg_func != 'none':
                        grouped = df.groupby(x_col)[y_col].agg(agg_func)
                    else:
                        grouped = df.groupby(x_col)[y_col].first()
                else:
                    grouped = df[x_col].value_counts()
                chart_result = {
                    'chartType': 'column',
                    'title': f'{y_col or "Count"} by {x_col}',
                    'labels': [str(x) for x in grouped.index.tolist()],
                    'datasets': [{'label': y_col or 'Count', 'data': [float(v) if pd.notna(v) else 0 for v in grouped.values.tolist()]}],
                }

            # Cap at MAX_CHART_POINTS
            if len(chart_result['labels']) > MAX_CHART_POINTS:
                chart_result['labels'] = chart_result['labels'][:MAX_CHART_POINTS]
                chart_result['datasets'][0]['data'] = chart_result['datasets'][0]['data'][:MAX_CHART_POINTS]

            # Build data table
            if y_col:
                display_cols = [x_col, y_col]
            else:
                display_cols = [x_col]
            table_df = df[display_cols].head(MAX_CHART_POINTS)

            data_payload = sanitize_for_json({
                'columns': table_df.columns.tolist(),
                'rows': table_df.values.tolist(),
                'row_count': len(df),
            })

            logger.info(f"[VISUALIZE] {chart_type} chart: {x_col} vs {y_col or 'count'}")
            return JsonResponse({
                'status': 'success',
                'chart': sanitize_for_json(chart_result),
                'data': data_payload,
            })

        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid request format.'})
        except Exception as e:
            logger.error(f"[VISUALIZE] Error: {traceback.format_exc()}")
            return JsonResponse({'status': 'error', 'message': str(e)})
