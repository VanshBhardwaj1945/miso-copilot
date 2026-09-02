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

## Dependencies

React + plain CSS only — `react`, `react-dom`, `vite`, `@vitejs/plugin-react`
(all MIT). No UI libraries.
