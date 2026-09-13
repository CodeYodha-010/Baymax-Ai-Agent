"""Query planner for Baymax TableAgent.

Decomposes COMPLEX queries into 1-5 ordered, independently-executable pandas
steps. Runs ONLY when the router already declined (intent == "complex") or
the user forces it via the "Deep Plan" toggle. Any failure falls back to the
plain code-gen loop in views.py — the planner can never break a query.

Output contract (strict JSON):
  {"steps": [{"id": 1, "goal": "...", "kind": "filter|aggregate|rank|compare|trend|lookup"}],
   "needs_plan": true, "confidence": 0.0-1.0}
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

PLANNER_CONFIDENCE_THRESHOLD = 0.7
PLANNER_MAX_STEPS = 5

PLANNER_SYSTEM_PROMPT = """You are a query planner for a tabular data assistant. You NEVER answer the question. You ONLY decompose it into ordered pandas steps, as strict JSON.

Columns available: {columns}

Return ONLY valid JSON (no markdown, no explanation) with exactly these keys:
{{"needs_plan": <true|false>, "steps": [{{"id": <int>, "goal": "<one sentence>", "kind": "<filter|aggregate|rank|compare|trend|lookup>"}}], "confidence": <0.0-1.0>}}

Rules:
- Each step must be independently executable pandas on the full dataframe `df` (which is preloaded; do NOT reload files).
- Step 1 narrows/selects the relevant data (filter or pick columns). The final step produces the answer object assigned to `result`.
- Order matters: steps run top to bottom, each seeing a short summary of prior step results.
- Max 5 steps, min 1. If the model later gets fewer, truncate — never invent filler steps.
- If the query is actually simple (single aggregation, top N, counts, show rows, null check) set needs_plan=false with empty steps.
- kinds: filter (subset rows), aggregate (group/sum/mean), rank (sort/top N), compare (side-by-side groups), trend (over time), lookup (find specific rows/values).
- confidence 0.7+ only if the decomposition is sound, <=0.5 if guessing.
"""


def _extract_content(reply):
    """Extract assistant text from LLMClient._call()'s model_dump() dict.

    Mirrors router._extract_content. Returns "" on any failure — the caller
    treats empty text as "no usable reply" and falls back. Never raises.
    """
    try:
        if not isinstance(reply, dict) or "error" in reply:
            return ""
        choices = reply.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message", {}) or {}
        content = msg.get("content", "") or ""
        return content if isinstance(content, str) else ""
    except Exception:
        return ""


def _build_messages(system: str, query) -> list:
    """Build a proper OpenAI messages list (system + user)."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "Question: " + str(query)},
    ]


def _parse_planner_json(text):
    """Extract JSON object from planner LLM reply (raw, fenced, or noisy).

    Returns None for non-string input — never raises.
    """
    if not text or not isinstance(text, str):
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


def sanitize_plan(data):
    """Validate + normalize a parsed plan dict. Returns None if unusable."""
    if not isinstance(data, dict):
        return None
    if data.get("needs_plan") is not True:
        return None
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    valid_kinds = {"filter", "aggregate", "rank", "compare", "trend", "lookup"}
    clean = []
    for i, s in enumerate(steps[:PLANNER_MAX_STEPS], start=1):
        if not isinstance(s, dict) or not str(s.get("goal", "")).strip():
            continue
        kind = s.get("kind") if s.get("kind") in valid_kinds else "lookup"
        clean.append({"id": i, "goal": str(s["goal"]).strip()[:500], "kind": kind})
    if not clean:
        return None
    try:
        conf = float(data.get("confidence", 0.0))
    except Exception:
        conf = 0.0
    return {"steps": clean, "confidence": conf}


class QueryPlanner:
    """Decomposer: query + schema -> validated step list. Uses views.LLMClient."""

    def __init__(self, client):
        self.client = client

    def plan(self, query, schema):
        cols = schema.get("columns", []) if isinstance(schema, dict) else []
        names = [c.get("name") for c in cols if isinstance(c, dict) and c.get("name")] or list(cols)
        system = PLANNER_SYSTEM_PROMPT.format(columns=", ".join([str(c) for c in names]) or "(unknown)")
        try:
            reply = self.client._call(_build_messages(system, query))
        except Exception as e:
            logger.warning("[PLANNER] LLM call failed: %s", e)
            return None
        text = _extract_content(reply)
        plan = sanitize_plan(_parse_planner_json(text) if isinstance(text, str) else None)
        if plan is None:
            logger.info("[PLANNER] No usable plan (needs_plan=false or invalid JSON)")
            return None
        if plan["confidence"] < PLANNER_CONFIDENCE_THRESHOLD:
            logger.info("[PLANNER] Plan confidence %.2f below threshold", plan["confidence"])
            return None
        logger.info("[PLANNER] Plan accepted: %d steps (conf %.2f)", len(plan["steps"]), plan["confidence"])
        return plan
