# Baymax AI Agent — Visualization System Context

This document explains the complete visualization architecture of the Baymax AI Agent. It is intended to give another AI (or developer) full context about how charts are generated, transformed, and rendered.

---

## 1. High-Level Architecture

```
User asks a question in chat
        ↓
LLM generates pandas code (may include plotting code)
        ↓# Baymax AI Agent — Visualization System Context

This document explains the complete visualization architecture of the Baymax AI Agent. It is intended to give another AI (or developer) full context about how charts are generated, transformed, and rendered.

---

## 1. High-Level Architecture

```
User asks a question in chat
        ↓
LLM generates pandas code (may include plotting code)
        ↓
[BACKEND] strip_matplotlib() — TRANSFORMS plotting code into data assignments
        ↓
[BACKEND] exec() runs the transformed code
        ↓
[BACKEND] extract_chart_data() — detects chart type from data shape
        ↓
[BACKEND] SSE event: {"type": "chart", "chart": {...}}
        ↓
[FRONTEND] ask.html receives SSE event
        ↓
[FRONTEND] charts.js → Highcharts renders the chart in chat
        ↓
[FRONTEND] Click chart → full-screen modal with zoom/pan/export
```

**Key principle:** The LLM is only a *code generator*. The backend is the *brain* for presentation — it transforms whatever code the LLM writes, detects the best chart type from the data shape, and sends a clean JSON config to the frontend. The frontend (Highcharts) does the actual rendering.

---

## 2. The Data Flow in Detail

### Step 1: LLM Generates Code
The LLM receives the dataset schema + sample data + the user's question. It writes pandas code. The system prompt tells it:
- ALWAYS assign the final answer to a variable named `result`
- NEVER use matplotlib/seaborn/plotly (but the LLM sometimes ignores this)
- For visual questions, return a DataFrame/Series as `result`

### Step 2: Code Transformation (`strip_matplotlib()` in views.py)
This is the **defensive layer**. Even if the LLM ignores the prompt and writes plotting code, this function transforms it:

| LLM Writes | Backend Transforms To |
|-----------|----------------------|
| `df.groupby('month')['revenue'].sum().plot(kind='bar')` | `result = df.groupby('month')['revenue'].sum()` |
| `df['revenue'].hist()` | `result = df['revenue']` |
| `df.plot(x='month', y='revenue')` | `result = df[['month', 'revenue']]` |
| `plt.show()` / `plt.savefig()` | (removed) |
| `import matplotlib` / `import plotly` | (removed) |

It uses regex to find `.plot()`, `.hist()`, `.boxplot()` calls and rewrites them to assign the data expression to `result`. This preserves the data so a chart can still be rendered.

### Step 3: Code Execution
The transformed code runs via `exec()` with `df`, `pd`, `np` available. The `result` variable holds the output (DataFrame, Series, or string).

### Step 4: Chart Type Detection (`extract_chart_data()` in views.py)
This function inspects the `result` and decides the chart type based on **data shape** (not keywords):

| Data Shape | Chart Type |
|-----------|-----------|
| DataFrame with `open`, `high`, `low`, `close` columns | **candlestick** (share market style) |
| Series with numeric values | **bar** (or line if time-indexed) |
| DataFrame with 1 non-numeric + 1 numeric column | **bar** (or pie if ≤8 rows) |
| DataFrame with 1 non-numeric + multiple numeric columns | **bar** (multi-series) |
| DataFrame with all numeric columns | **bar** or **line** (uses row index as labels) |
| DataFrame with 2 numeric columns | **scatter** |
| Single-column DataFrame | **bar** (uses index as labels) |

The output is a JSON config:
```json
{
  "chartType": "candlestick",
  "title": "OHLC Chart",
  "labels": ["2023-01", "2023-02", ...],
  "datasets": [{"label": "OHLC", "data": [[open, high, low, close], ...]}]
}
```

### Step 5: SSE Event
The backend sends this as a Server-Sent Event:
```
data: {"type": "chart", "chart": {...}}
```

### Step 6: Frontend Rendering (`charts.js`)
The `ask.html` receives the SSE event and calls `BaymaxCharts.renderCompact(containerId, chartConfig)`. This:
1. Maps the `chartType` to a Highcharts type (bar→column, candlestick→candlestick, etc.)
2. Builds the Highcharts options object
3. Renders a compact chart (170px height) in the chat bubble
4. Clicking the compact chart opens a full-screen modal with zoom/pan/export

---

## 3. Supported Chart Types

| chartType | Highcharts Type | Notes |
|-----------|----------------|-------|
| `bar` | `column` | Vertical bars, data labels on top |
| `line` | `line` | Line chart with markers |
| `pie` | `pie` | Pie with percentage labels |
| `scatter` | `scatter` | Scatter plot, larger markers on hover |
| `histogram` | `column` | Histogram (binned data) |
| `area` | `area` | Area chart |
| `heatmap` | `heatmap` | Heatmap (requires heatmap module) |
| `candlestick` | `candlestick` | OHLC candles, green up / red down |

---

## 4. Interactivity (All Charts)

All charts have these interactive features (configured in `charts.js` `buildHighchartsOpts()`):
- **Zoom** — drag to zoom into any region (XY zoom)
- **Pan** — hold Shift + drag to pan
- **Reset zoom button** — styled button to reset
- **Legend** — click to show/hide series
- **Crosshairs** — horizontal + vertical lines on hover
- **Shared tooltips** — hover shows all series values
- **Data labels** — values on top of bars/columns
- **Export menu** — in modal: PNG, JPEG, SVG, PDF, print
- **Point click events** — infrastructure for click-to-ask follow-ups

---

## 5. Key Files

| File | Role |
|------|------|
| `Baymax/apps/TableAgent/views.py` | Backend: LLM client, code transformation, chart detection, SSE |
| `Baymax/static/charts.js` | Frontend: Highcharts rendering, type mapping, interactivity |
| `Baymax/templates/base.html` | Loads Highcharts core + modules (candlestick, heatmap, exporting) |
| `Baymax/apps/TableAgent/templates/ask.html` | Chat UI: receives SSE events, renders charts/tables |
| `Baymax/apps/TableAgent/templates/visualize.html` | Manual Chart Builder page (separate from AI chat) |
| `Baymax/static/style.css` | Styling for charts, tables, markdown, raw data sections |

---

## 6. Key Functions in views.py

### `strip_matplotlib(code)` — Code Transformation
- Input: raw LLM-generated code
- Output: transformed code with `.plot()` calls rewritten to `result = <data>`
- Purpose: preserve data even when the LLM writes plotting code

### `extract_chart_data(result, suggested_type)` — Chart Detection
- Input: the `result` variable (DataFrame, Series, or string)
- Output: chart config dict, or `None` if not chartable
- Purpose: detect the best chart type from data shape

### `extract_df_payload(result)` — Table Data
- Input: the `result` variable
- Output: `{columns, rows, row_count}` for rendering a data table
- Purpose: show tabular data alongside charts

### `sanitize_for_json(obj)` — JSON Safety
- Recursively converts pandas/numpy/datetime types to JSON-safe types
- Timestamps → ISO strings, NaN → null, numpy ints → Python ints
- Purpose: prevent `TypeError: Object of type Timestamp is not JSON serializable`

### `NumpyEncoder` — JSON Encoder Safety Net
- Used in `json.dumps(event, cls=NumpyEncoder)` in the SSE stream
- Handles any type that slips through `sanitize_for_json()`

---

## 7. Key Functions in charts.js

### `buildHighchartsOpts(chartConfig, opts)` — Chart Config Builder
- Maps `chartType` to Highcharts type
- Handles special data formats (candlestick `[open, high, low, close]`, pie `{name, y}`)
- Adds interactivity (zoom, pan, legend, crosshairs, tooltips, data labels, export)
- Returns the full Highcharts options object

### `render(containerId, chartConfig)` — Full Chart
- Renders a chart in a container (used by Chart Builder page)

### `renderCompact(containerId, chartConfig)` — In-Chat Chart
- Renders a 170px compact chart in the chat bubble

### `openModal(chartConfig)` — Full-Screen Modal
- Opens a modal with the full-size chart + export menu

### `renderTable(containerId, payload)` — Data Table
- Renders a styled HTML table from `{columns, rows}`

---

## 8. The Hybrid Approach (Current State)

The system uses a **hybrid approach**:
1. **Primary:** LLM generates pandas code → backend transforms → executes → detects chart → renders
2. **Future (planned):** A "recipe book" of pre-built operations (groupby, filter, sort, etc.) where the LLM returns structured JSON instead of code. This would eliminate arbitrary code execution (fixing the RCE security vulnerability) and make chart type explicit.

The current implementation is the flexible code-generation approach with defensive transformation. The recipe book is the recommended future migration.

---

## 9. Known Limitations

- **RCE vulnerability:** `exec()` runs LLM-generated code with full Python privileges. This is a security risk if exposed to the internet. The recipe book approach (Section 8) would fix this.
- **Code transformation is regex-based:** It handles common `.plot()` patterns but not every possible code structure.
- **Free LLM model** (`ling-3.0-flash:free`) may not always follow prompt instructions — hence the defensive transformation layer.
- **No authentication:** Anyone with the URL can upload files and run queries.

---

## 10. How to Test

```bash
cd Baymax
python manage.py runserver
```

1. Upload a CSV/Excel file
2. Ask: *"visualize the data how monthly reviews go to high and low like share market"*
3. Expect: a **candlestick chart** (green/red candles) in the chat
4. Click the chart → full-screen modal with zoom/pan/export

Other test queries:
- "Show me revenue by category" → bar chart
- "What's the trend of sales over time?" → line chart
- "Compare revenue and cost by region" → multi-series bar chart
- "Show me a pie chart of order statuses" → pie chart
[BACKEND] strip_matplotlib() — TRANSFORMS plotting code into data assignments
        ↓
[BACKEND] exec() runs the transformed code
        ↓
[BACKEND] extract_chart_data() — detects chart type from data shape
        ↓
[BACKEND] SSE event: {"type": "chart", "chart": {...}}
        ↓
[FRONTEND] ask.html receives SSE event
        ↓
[FRONTEND] charts.js → Highcharts renders the chart in chat
        ↓
[FRONTEND] Click chart → full-screen modal with zoom/pan/export
```

**Key principle:** The LLM is only a *code generator*. The backend is the *brain* for presentation — it transforms whatever code the LLM writes, detects the best chart type from the data shape, and sends a clean JSON config to the frontend. The frontend (Highcharts) does the actual rendering.

---

## 2. The Data Flow in Detail

### Step 1: LLM Generates Code
The LLM receives the dataset schema + sample data + the user's question. It writes pandas code. The system prompt tells it:
- ALWAYS assign the final answer to a variable named `result`
- NEVER use matplotlib/seaborn/plotly (but the LLM sometimes ignores this)
- For visual questions, return a DataFrame/Series as `result`

### Step 2: Code Transformation (`strip_matplotlib()` in views.py)
This is the **defensive layer**. Even if the LLM ignores the prompt and writes plotting code, this function transforms it:

| LLM Writes | Backend Transforms To |
|-----------|----------------------|
| `df.groupby('month')['revenue'].sum().plot(kind='bar')` | `result = df.groupby('month')['revenue'].sum()` |
| `df['revenue'].hist()` | `result = df['revenue']` |
| `df.plot(x='month', y='revenue')` | `result = df[['month', 'revenue']]` |
| `plt.show()` / `plt.savefig()` | (removed) |
| `import matplotlib` / `import plotly` | (removed) |

It uses regex to find `.plot()`, `.hist()`, `.boxplot()` calls and rewrites them to assign the data expression to `result`. This preserves the data so a chart can still be rendered.

### Step 3: Code Execution
The transformed code runs via `exec()` with `df`, `pd`, `np` available. The `result` variable holds the output (DataFrame, Series, or string).

### Step 4: Chart Type Detection (`extract_chart_data()` in views.py)
This function inspects the `result` and decides the chart type based on **data shape** (not keywords):

| Data Shape | Chart Type |
|-----------|-----------|
| DataFrame with `open`, `high`, `low`, `close` columns | **candlestick** (share market style) |
| Series with numeric values | **bar** (or line if time-indexed) |
| DataFrame with 1 non-numeric + 1 numeric column | **bar** (or pie if ≤8 rows) |
| DataFrame with 1 non-numeric + multiple numeric columns | **bar** (multi-series) |
| DataFrame with all numeric columns | **bar** or **line** (uses row index as labels) |
| DataFrame with 2 numeric columns | **scatter** |
| Single-column DataFrame | **bar** (uses index as labels) |

The output is a JSON config:
```json
{
  "chartType": "candlestick",
  "title": "OHLC Chart",
  "labels": ["2023-01", "2023-02", ...],
  "datasets": [{"label": "OHLC", "data": [[open, high, low, close], ...]}]
}
```

### Step 5: SSE Event
The backend sends this as a Server-Sent Event:
```
data: {"type": "chart", "chart": {...}}
```

### Step 6: Frontend Rendering (`charts.js`)
The `ask.html` receives the SSE event and calls `BaymaxCharts.renderCompact(containerId, chartConfig)`. This:
1. Maps the `chartType` to a Highcharts type (bar→column, candlestick→candlestick, etc.)
2. Builds the Highcharts options object
3. Renders a compact chart (170px height) in the chat bubble
4. Clicking the compact chart opens a full-screen modal with zoom/pan/export

---

## 3. Supported Chart Types

| chartType | Highcharts Type | Notes |
|-----------|----------------|-------|
| `bar` | `column` | Vertical bars, data labels on top |
| `line` | `line` | Line chart with markers |
| `pie` | `pie` | Pie with percentage labels |
| `scatter` | `scatter` | Scatter plot, larger markers on hover |
| `histogram` | `column` | Histogram (binned data) |
| `area` | `area` | Area chart |
| `heatmap` | `heatmap` | Heatmap (requires heatmap module) |
| `candlestick` | `candlestick` | OHLC candles, green up / red down |

---

## 4. Interactivity (All Charts)

All charts have these interactive features (configured in `charts.js` `buildHighchartsOpts()`):
- **Zoom** — drag to zoom into any region (XY zoom)
- **Pan** — hold Shift + drag to pan
- **Reset zoom button** — styled button to reset
- **Legend** — click to show/hide series
- **Crosshairs** — horizontal + vertical lines on hover
- **Shared tooltips** — hover shows all series values
- **Data labels** — values on top of bars/columns
- **Export menu** — in modal: PNG, JPEG, SVG, PDF, print
- **Point click events** — infrastructure for click-to-ask follow-ups

---

## 5. Key Files

| File | Role |
|------|------|
| `Baymax/apps/TableAgent/views.py` | Backend: LLM client, code transformation, chart detection, SSE |
| `Baymax/static/charts.js` | Frontend: Highcharts rendering, type mapping, interactivity |
| `Baymax/templates/base.html` | Loads Highcharts core + modules (candlestick, heatmap, exporting) |
| `Baymax/apps/TableAgent/templates/ask.html` | Chat UI: receives SSE events, renders charts/tables |
| `Baymax/apps/TableAgent/templates/visualize.html` | Manual Chart Builder page (separate from AI chat) |
| `Baymax/static/style.css` | Styling for charts, tables, markdown, raw data sections |

---

## 6. Key Functions in views.py

### `strip_matplotlib(code)` — Code Transformation
- Input: raw LLM-generated code
- Output: transformed code with `.plot()` calls rewritten to `result = <data>`
- Purpose: preserve data even when the LLM writes plotting code

### `extract_chart_data(result, suggested_type)` — Chart Detection
- Input: the `result` variable (DataFrame, Series, or string)
- Output: chart config dict, or `None` if not chartable
- Purpose: detect the best chart type from data shape

### `extract_df_payload(result)` — Table Data
- Input: the `result` variable
- Output: `{columns, rows, row_count}` for rendering a data table
- Purpose: show tabular data alongside charts

### `sanitize_for_json(obj)` — JSON Safety
- Recursively converts pandas/numpy/datetime types to JSON-safe types
- Timestamps → ISO strings, NaN → null, numpy ints → Python ints
- Purpose: prevent `TypeError: Object of type Timestamp is not JSON serializable`

### `NumpyEncoder` — JSON Encoder Safety Net
- Used in `json.dumps(event, cls=NumpyEncoder)` in the SSE stream
- Handles any type that slips through `sanitize_for_json()`

---

## 7. Key Functions in charts.js

### `buildHighchartsOpts(chartConfig, opts)` — Chart Config Builder
- Maps `chartType` to Highcharts type
- Handles special data formats (candlestick `[open, high, low, close]`, pie `{name, y}`)
- Adds interactivity (zoom, pan, legend, crosshairs, tooltips, data labels, export)
- Returns the full Highcharts options object

### `render(containerId, chartConfig)` — Full Chart
- Renders a chart in a container (used by Chart Builder page)

### `renderCompact(containerId, chartConfig)` — In-Chat Chart
- Renders a 170px compact chart in the chat bubble

### `openModal(chartConfig)` — Full-Screen Modal
- Opens a modal with the full-size chart + export menu

### `renderTable(containerId, payload)` — Data Table
- Renders a styled HTML table from `{columns, rows}`

---

## 8. The Hybrid Approach (Current State)

The system uses a **hybrid approach**:
1. **Primary:** LLM generates pandas code → backend transforms → executes → detects chart → renders
2. **Future (planned):** A "recipe book" of pre-built operations (groupby, filter, sort, etc.) where the LLM returns structured JSON instead of code. This would eliminate arbitrary code execution (fixing the RCE security vulnerability) and make chart type explicit.

The current implementation is the flexible code-generation approach with defensive transformation. The recipe book is the recommended future migration.

---

## 9. Known Limitations

- **RCE vulnerability:** `exec()` runs LLM-generated code with full Python privileges. This is a security risk if exposed to the internet. The recipe book approach (Section 8) would fix this.
- **Code transformation is regex-based:** It handles common `.plot()` patterns but not every possible code structure.
- **Free LLM model** (`ling-3.0-flash:free`) may not always follow prompt instructions — hence the defensive transformation layer.
- **No authentication:** Anyone with the URL can upload files and run queries.

---

## 10. How to Test

```bash
cd Baymax
python manage.py runserver
```

1. Upload a CSV/Excel file
2. Ask: *"visualize the data how monthly reviews go to high and low like share market"*
3. Expect: a **candlestick chart** (green/red candles) in the chat
4. Click the chart → full-screen modal with zoom/pan/export

Other test queries:
- "Show me revenue by category" → bar chart
- "What's the trend of sales over time?" → line chart
- "Compare revenue and cost by region" → multi-series bar chart
- "Show me a pie chart of order statuses" → pie chart