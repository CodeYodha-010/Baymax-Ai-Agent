"""Router + Deterministic executor for Baymax TableAgent.

Fast-path for simple queries: a cheap LLM call classifies the query into
strict JSON, then pure pandas (NO exec) computes the answer. Friendly
language still comes from LLMClient.humanize_result (step 2).

If router confidence is low or intent is complex, caller falls back to the
original full code-generation agent in views.py (untouched logic).
"""
import json
import logging
import re
from difflib import get_close_matches

logger = logging.getLogger(__name__)

ROUTER_CONFIDENCE_THRESHOLD = 0.85

ROUTER_SYSTEM_PROMPT = """You are a query router for a tabular data assistant. You NEVER answer. You ONLY classify into strict JSON.

Columns available: {columns}

Return ONLY valid JSON (no markdown, no explanation) with exactly these keys:
{{"intent": "<greeting|aggregation|top_n|bottom_n|count_filter|value_counts|null_check|describe|show_rows|complex>", "op": "<mean|sum|min|max|count|median|null>", "column": "<column name or null>", "filter_col": "<column name or null>", "filter_val": "<value or null>", "limit": <int or null>, "confidence": <0.0-1.0>}}

Rules:
- aggregation: single number (average/mean/avg/sum/total/min/max/median/count of a column). Default op=mean.
- If user asks "how many rows/records/entries are in the dataset" (no specific column): {{"intent":"aggregation","op":"count","column":null,"confidence":0.95}}
- top_n/bottom_n: highest/lowest/top/bottom N rows. Set limit (default 10). column=sort column if mentioned else null.
- count_filter: count rows matching condition ("how many status=active?"). Set filter_col+filter_val.
- value_counts: distribution per value ("count by category"). column=group column.
- null_check: missing/null/empty questions. column if mentioned else null.
- describe: overview/summary/stats of dataset or column.
- show_rows: see raw rows ("show top 10", "head", "display first 5").
- greeting: hello/hi/thanks/bye, no data question.
- complex: trend/time-series/correlation/outlier/compare/plot/multi-step or anything else.
- Synonyms: avg/average/mean->mean, total->sum, highest/largest->top_n, lowest/smallest->bottom_n.
- Fuzzy-map column names to provided list. If unsure set column=null, confidence<=0.5.
- confidence 1.0 only if crystal clear, 0.7-0.9 if likely, <=0.5 if guessing.
"""


def resolve_column(name, columns):
    """Fuzzy-resolve a column name to the real df column. None if no match."""
    if not name or columns is None:
        return None
    cols = list(columns)
    low = {str(c).lower(): c for c in cols}
    if str(name).lower() in low:
        return low[str(name).lower()]
    for c in cols:
        if str(name).lower() in str(c).lower() or str(c).lower() in str(name).lower():
            return c
    m = get_close_matches(str(name), [str(c) for c in cols], n=1, cutoff=0.6)
    return m[0] if m else None


def _parse_router_json(text):
    """Extract JSON object from router LLM reply (raw, fenced, or noisy)."""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1)
    else:
        s, e = t.find("{"), t.rfind("}")
        if s != -1 and e != -1 and e > s:
            t = t[s:e + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


class LLMRouter:
    """Cheap classifier: query + schema -> route dict. Uses views.LLMClient."""

    def __init__(self, client):
        self.client = client

    def route(self, query, schema):
        cols = schema.get("columns", []) if isinstance(schema, dict) else []
        names = [c.get("name") for c in cols if isinstance(c, dict) and c.get("name")] or list(cols)
        system = ROUTER_SYSTEM_PROMPT.format(columns=", ".join([str(c) for c in names]) or "(unknown)")
        try:
            reply = self.client._call(system, "Question: " + str(query))
        except Exception as e:
            logger.warning("[ROUTER] LLM call failed: %s", e)
            return {"intent": "complex", "confidence": 0.0}
        data = _parse_router_json(reply or "")
        if not isinstance(data, dict):
            return {"intent": "complex", "confidence": 0.0}
        valid = {"greeting", "aggregation", "top_n", "bottom_n", "count_filter",
                 "value_counts", "null_check", "describe", "show_rows", "complex"}
        if data.get("intent") not in valid:
            data["intent"] = "complex"
        try:
            data["confidence"] = float(data.get("confidence", 0.0))
        except Exception:
            data["confidence"] = 0.0
        return data


class DeterministicExecutor:
    """Pure-pandas executor for routed intents. NO exec(), NO LLM."""

    def __init__(self, df):
        self.df = df

    def _apply_filter(self, df, filter_col, filter_val):
        if filter_col and filter_val is not None and filter_col in df.columns:
            try:
                return df[df[filter_col].astype(str).str.lower() == str(filter_val).lower()]
            except Exception:
                try:
                    return df[df[filter_col] == filter_val]
                except Exception:
                    return df
        return df

    def run(self, route):
        """Returns {ok, raw_output(str), result(DF|Series|str|None)}."""
        import pandas as pd

        df = self.df
        route = route or {}
        intent = route.get("intent", "complex")
        op = route.get("op") or None
        col = resolve_column(route.get("column"), df.columns)
        fcol = resolve_column(route.get("filter_col"), df.columns)
        fval = route.get("filter_val")
        try:
            limit = int(route.get("limit") or 10)
        except Exception:
            limit = 10
        limit = max(1, min(limit, 100))

        if intent == "greeting":
            msg = "Hello! Ask me about averages, top rows, counts, or an overview of your data."
            return {"ok": True, "raw_output": msg, "result": msg}

        if intent == "aggregation" and op == "count" and not col:
            # Dataset-level stats: "how many rows are there?"
            raw = "Dataset has %d rows and %d columns." % (len(df), len(df.columns))
            return {"ok": True, "raw_output": raw, "result": raw}

        if intent == "aggregation" and col and col in df.columns:
            series = self._apply_filter(df, fcol, fval)[col]
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            label = "%s(%s)" % (op or "mean", col)
            if fcol and fval is not None:
                label += " where %s=%s" % (fcol, fval)
            if op in ("mean", None):
                v = numeric.mean() if len(numeric) else float("nan")
            elif op == "sum":
                v = numeric.sum() if len(numeric) else 0
            elif op == "min":
                v = numeric.min() if len(numeric) else float("nan")
            elif op == "max":
                v = numeric.max() if len(numeric) else float("nan")
            elif op == "median":
                v = numeric.median() if len(numeric) else float("nan")
            elif op == "count":
                v = int(series.count())
            else:
                v = numeric.mean() if len(numeric) else float("nan")
            if op == "count":
                raw = "%s = %d (non-null values)" % (label, int(v))
            else:
                raw = ("%s = %.4f" % (label, v)) if pd.notna(v) else ("%s: no numeric data" % label)
            return {"ok": True, "raw_output": raw, "result": raw}

        if intent in ("top_n", "bottom_n", "show_rows"):
            out = self._apply_filter(df, fcol, fval)
            if col and col in out.columns:
                try:
                    out = out.sort_values(by=col, ascending=(intent == "bottom_n")).head(limit)
                except Exception:
                    out = out.head(limit)
            else:
                out = out.head(limit) if intent != "bottom_n" else out.tail(limit)
            return {"ok": True, "raw_output": out.to_string(index=False), "result": out}

        if intent == "count_filter":
            out = self._apply_filter(df, fcol, fval)
            raw = "Count where %s=%s: %d rows (of %d total)" % (fcol, fval, len(out), len(df))
            return {"ok": True, "raw_output": raw, "result": raw}

        if intent == "value_counts":
            if col and col in df.columns:
                vc = self._apply_filter(df, fcol, fval)[col].astype(str).value_counts().head(50)
                return {"ok": True, "raw_output": vc.to_string(), "result": vc}
            return {"ok": False, "raw_output": "", "result": None}

        if intent == "null_check":
            if col and col in df.columns:
                n = int(df[col].isna().sum())
                pct = 100.0 * n / max(len(df), 1)
                raw = "Missing values in '%s': %d of %d rows (%.1f%%)" % (col, n, len(df), pct)
                return {"ok": True, "raw_output": raw, "result": raw}
            nulls = df.isna().sum()
            nulls = nulls[nulls > 0].sort_values(ascending=False).head(20)
            raw = ("Missing values per column:\n" + nulls.to_string()) if len(nulls) else "No missing values found."
            return {"ok": True, "raw_output": raw, "result": nulls if len(nulls) else raw}

        if intent == "describe":
            if col and col in df.columns:
                raw = df[col].describe(include="all").to_string()
                return {"ok": True, "raw_output": raw, "result": raw}
            parts = ["Shape: %d rows x %d columns" % (df.shape[0], df.shape[1]),
                     "Columns: " + ", ".join([str(c) for c in df.columns])]
            try:
                parts.append(df.describe(include="all").to_string())
            except Exception:
                pass
            raw = "\n".join(parts)
            return {"ok": True, "raw_output": raw, "result": raw}

        return {"ok": False, "raw_output": "", "result": None}
