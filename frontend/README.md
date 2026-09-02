# MISO Copilot — React frontend

The demo UI: a static MISO-style landing page (`MisoLandingPage.jsx`) with the
interactive Copilot chat panel (`MisoCopilot.jsx`) docked in the upper-right.

Design rules and the locked color palette live in [`UI_RULES.md`](UI_RULES.md) —
read it before touching any styling.

## Run

```bash
npm install
npm run dev        # http://localhost:5173
```

`/ask` is proxied to the FastAPI backend on `localhost:8000` (see
`vite.config.js`). Until the backend is running, asking a question shows the
graceful-handoff message with a link to MISO's contact form.

## Backend contract

```
POST /ask  {"question": "..."}
→ {"answer": "...", "sources": [{"title": "...", "url": "..."}], "as_of": "6:55 PM EST"}
```

## Dependencies

React + plain CSS only — `react`, `react-dom`, `vite`, `@vitejs/plugin-react`
(all MIT). No UI libraries.
