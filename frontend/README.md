# MISO Copilot — React frontend

The demo UI: a static MISO-style landing page (`src/fake-landingpage/`) with the
interactive Copilot chat panel (`src/copilot/`) docked in the bottom-right. The
panel is resizable via the grip in its top-left corner, and the input grows as
you type (Enter sends, Shift+Enter for a new line).

Design rules and the locked color palette live in [`UI_RULES.md`](UI_RULES.md) —
read it before touching any styling.

## Run

```bash
npm install
npm run dev        # http://localhost:5173
```

`/ask` is proxied to the FastAPI backend on `localhost:8000` (see
`vite.config.js`) — start it with `uvicorn backend.main:app --reload --port 8000`
from the repo root (needs `CLAUDE_API_KEY` in `../.env`). If the backend is down
or unconfigured, asking a question shows the graceful-handoff message with a
link to MISO's contact form.

## Backend contract

```
POST /ask  {"question": "..."}
→ {"answer": "...", "sources": [{"title": "...", "url": "..."}], "as_of": "6:55 PM EST"}
```

## Answer rendering

Assistant answers render rich content (`src/copilot/Markdown.jsx`):

- Full GFM markdown: **bold**, tables, lists, inline links, code blocks
- LaTeX math via KaTeX: `$...$` / `$$...$$` (real fractions and symbols)
- Charts via Recharts: the backend emits ` ```chart ` fenced JSON blocks
  (`type`: line / bar / area / pie) that `ChartBlock.jsx` renders with a
  fixed colorblind-safe series palette

## Dependencies

All MIT, no UI component libraries: `react`, `react-dom`, `vite`,
`@vitejs/plugin-react`, `react-markdown`, `remark-gfm`, `remark-math`,
`rehype-katex`, `katex`, `recharts`.
