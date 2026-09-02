# MISO Copilot — UI rules (for any agent touching `frontend/`)

## 1. Overall feeling

Professional, institutional, modern, trustworthy. It should feel like a
**MISO product**, not a consumer AI app.

## 2. Color palette (locked)

| Role | Hex |
|---|---|
| Deep MISO Navy | `#092A47` |
| Primary MISO Blue | `#087CC1` |
| Bright Utility Blue | `#1496D4` |
| MISO Green (accent) | `#65B82E` |
| Dark text | `#122B40` |
| Muted/secondary text | `#526575` |
| Border | `#DCE5EB` |
| Background | `#FFFFFF` |
| Soft background | `#F6F9FB` |
| Light blue tint | `#EDF6FB` |

## 3. DO NOT use

- Purple, neon gradients, rainbow gradients
- Excessive glassmorphism or animations
- Huge rounded "pill" UI
- Dark-mode chatbot styling
- Generic ChatGPT-looking UI
- MISO's real logo or copied site assets (approximate only)

## 4. Copilot panel

Fixed **bottom-right** (opens from the launcher button), default ~360×520px,
resizable via the top-left grip, white background, 1px subtle border, 14–16px
radius, restrained navy shadow. Mobile: near full-width, resize grip hidden.

## 5. Header

"MISO Copilot" + small sparkle icon in a navy circle; close button on the
right; clean 64–72px header.

## 6. Suggested questions

White cards, thin gray-blue borders, small blue icon, right-facing arrow,
subtle hover. Clicking one submits it immediately.

## 7. Input

White rounded rectangular input, navy circular send button, blue focus ring.

## 8. Messages

Assistant replies left-aligned on very light blue/gray backgrounds; user
messages compact navy, right side. Never huge colored bubbles.

**Product rules (graded, from `../AGENTS.md`):**
- Every answer renders its **source link(s)** as chips — the link IS the product.
- Live-data answers render an **"as of \<time\>"** stamp — staleness stays visible.
- Backend failure / no answer → graceful handoff to MISO's contact form.
  Never a hallucinated answer, never a dead end.

**Rich content** (assistant messages only; user messages stay plain text):
- Answers render full GFM markdown via `Markdown.jsx`: bold, tables (in a
  horizontal-scroll wrapper), inline links (open in new tab), code blocks.
- LaTeX math renders via KaTeX (`$...$` inline, `$$...$$` display) — real
  fractions and symbols, never plain-text approximations.
- ` ```chart ` fenced blocks render as Recharts charts via `ChartBlock.jsx`.
  Spec: `{"type":"line|bar|area|pie","title","unit","labels":[...],
  "series":[{"name","data":[...]}]}`, max 4 series. Invalid JSON falls back
  to a code block.
- **Chart series palette (locked, fixed order, colorblind-validated):**
  `#087CC1` blue → `#B36F0E` amber → `#00879E` teal → `#3E8814` green.
  Never reorder, cycle, or add hues; a 5th series folds into "Other".

## 9. Typography

Clean sans-serif: Inter (Google Fonts, OFL) with system-ui fallback.

## 10. Accessibility

All buttons have aria-labels; input has a proper label; keyboard navigation
works; never rely on color alone.

## 11. Dependencies

React + plain CSS for all layout/styling — no UI component libraries.
Approved rendering deps (all MIT): `react`, `react-dom`, `vite`,
`@vitejs/plugin-react`, `react-markdown`, `remark-gfm`, `remark-math`,
`rehype-katex`, `katex`, `recharts`. Anything else must be genuinely open
source with a permissive license.

## 12. Backend contract

`POST /ask` with `{"question": string}` →
`{"answer": string, "sources": [{"title","url"}], "as_of": "6:55 PM EST"}`.
The Vite dev server proxies `/ask` to FastAPI on `localhost:8000` (see
`vite.config.js`) — no CORS setup needed.
