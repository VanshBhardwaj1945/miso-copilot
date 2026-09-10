# Spec: a test suite for `frontend/`

**Status:** written 2026-09-10, nothing built. No tooling installed, no tests exist.
**Scope:** `frontend/src/**` - 842 lines across seven files.
**Companion reading:** [`../AGENTS.md`](../AGENTS.md) (conventions, definition of done),
[`../frontend/UI_RULES.md`](../frontend/UI_RULES.md) (the product rules the tests
actually protect), [`../pytest.ini`](../pytest.ini) (the Python lane's floor, which
this mirrors).

> **Do not start this on Sept 10.** Demo day is Sept 11 and `npm run dev` is the
> demo. Installing a test runner, a DOM implementation and a coverage provider
> the evening before is exactly how the demo stops booting. This lands Sept 12
> or later. The one thing that survives that rule is reading section 9 - the
> bugs are real today and three of them are two-line fixes.

---

## 1. Why this exists

The Python lane has a suite, a 90 percent floor, and CI that enforces both. The
React lane - which *is* the demo - has nothing. Today the only verification is
"the touched UI boots and a chat message round-trips" (AGENTS.md, *Run / verify*),
performed by a human, by hand, once.

Three of the things this frontend does are not the kind of thing a human catches
by looking at it:

1. **`MapBlock` filters model-controlled state codes against an allowlist**
   (`frontend/src/ramen/MapBlock.jsx:8`). The prompt is *told* the footprint;
   the renderer is the thing that *enforces* it. A backend review found the model
   can be pushed toward emitting out-of-footprint codes. Delete four lines from
   `MapBlock.jsx:50-54` and the map silently starts drawing whatever the model
   says - and it will look completely fine to whoever deleted them.
2. **`Markdown` renders model output as HTML** (`Markdown.jsx:14`). Raw HTML is
   escaped and `javascript:` hrefs are neutered only because nobody has added
   `rehype-raw` or a custom `urlTransform` yet. Both are one-line changes that a
   reasonable person might make to fix a rendering complaint.
3. **`ChartBlock` parses model-controlled JSON and hands it to recharts**
   (`ChartBlock.jsx:31`). One shape of valid JSON crashes the entire chat panel
   today. See section 9.

Everything else in this spec is coverage. Those three are the reason to build it.

---

## 2. Tooling

### 2.1 The recommendation

**Vitest + React Testing Library + jsdom + `@vitest/coverage-v8`.** All MIT, all
dev-only.

```
vitest
@vitest/coverage-v8          # version must match vitest exactly
@testing-library/react
@testing-library/dom         # RTL v16 peer, must be explicit
@testing-library/user-event
@testing-library/jest-dom
jsdom
```

These go in `devDependencies` and **nowhere near** `dependencies`. That is the
same rule as "`pytest` and `pytest-cov` stay out of `requirements.txt`"
(AGENTS.md, *Conventions*) and the same rule as UI_RULES.md §11, which governs
what ships - nothing here ships. Add a one-line comment in the PR saying so, so
the next person reading §11 does not think the approved-dependency list was
quietly widened.

### 2.2 Vitest over Jest, and it is not close

Jest is the wrong tool for *this* app, for reasons specific to what it imports:

- **The unified ecosystem is ESM-only.** `react-markdown@10`, `remark-gfm`,
  `remark-math`, `rehype-katex` and every `micromark`/`mdast`/`unified` package
  beneath them publish ESM with an exports map and no CJS fallback. Jest would
  need `transformIgnorePatterns` surgery listing dozens of transitive packages,
  and that list rots every time a dependency updates. Vitest runs them natively.
- **`Markdown.jsx:5` imports `katex/dist/katex.min.css`** and
  `MisoRamen.jsx:2,4` import PNGs. Jest needs `moduleNameMapper` stubs for both.
  Vitest inherits Vite's resolution: CSS is stubbed by default (`css: false`),
  assets resolve to a URL string. Zero configuration.
- **Vitest reuses the project's own transform.** The code that runs under test is
  transformed by the same `@vitejs/plugin-react` that builds the demo. A test
  passing means the *shipped* transform of that file passes, not Babel's
  approximation of it.

Jest's advantages here - a larger ecosystem and more Stack Overflow answers -
buy nothing that this six-file suite needs.

### 2.3 The one real risk: Vite 8 is rolldown-based

`frontend/package.json` pins `vite: ^8.3.0`, and `node_modules/vite/package.json`
shows it depends on `rolldown@~1.2.6`. Vitest shares the host project's Vite, so
its peer range matters:

**Before writing a single test, run this and read the answer:**

```bash
cd frontend
npm info vitest peerDependencies
npm info vitest version
```

- **If the peer range includes `^8.0.0`:** install normally, done. Verify with
  `npm ls vite` that exactly one Vite is resolved. This is the expected outcome -
  Vitest tracks Vite releases closely.
- **If it does not:** do **not** reach for `--legacy-peer-deps` and hope. In
  order of preference: (a) pin the first Vitest release whose peers list Vite 8;
  (b) if none exists yet, defer this work - it is not urgent enough to fight a
  bundler over; (c) as a last resort, install with the peer override *and* add a
  smoke test that imports every `src/` module, so a rolldown transform difference
  fails loudly rather than showing up as a mystery.

Note what is *not* a risk: `npm run build` and `npm run dev` are untouched by any
of this, provided you follow §2.5.

### 2.4 jsdom over happy-dom

happy-dom is roughly 2x faster. On a suite this size that is about one second,
which buys nothing, and it costs:

- **SVG.** `MapBlock` renders ~50 `<path>` elements with `<title>` children and
  is queried by `role="img"` and by attribute. jsdom's SVG and accessibility-tree
  behavior is the better-trodden path for RTL.
- **KaTeX** pokes at `getComputedStyle` and text metrics during render.
- **recharts** needs `ResizeObserver` (neither environment provides it - you
  polyfill it in setup either way) and reads `getBoundingClientRect`.

Pick jsdom. Revisit only if the suite ever takes more than ten seconds.

### 2.5 Config: a separate `vitest.config.js`, deliberately

Do **not** add a `test` block to `frontend/vite.config.js`. That file carries the
`/ask` and `/crosswalk.csv` dev proxy - it is load-bearing for the demo, and a
config error in it takes down `npm run dev`. Vitest prefers `vitest.config.js`
when both exist, so the two stay isolated.

**`frontend/vitest.config.js`** (new file):

```js
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Kept separate from vite.config.js on purpose: that file carries the dev
// proxy the demo depends on, and a broken test config must never break `npm run dev`.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./tests/setup.js"],
    include: ["tests/**/*.test.jsx"],
    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary", "lcov"],
      include: ["src/**/*.{js,jsx}"],
      exclude: [
        "src/main.jsx",            // DOM bootstrap; needs a real #root, protects nothing
        "src/**/*.css",
      ],
      thresholds: {
        lines: 90,
        statements: 90,
        functions: 90,
        branches: 85,
        // The landing page is 123 lines of free coverage (see §3). This keeps
        // it from carrying the headline number for the code that matters.
        "src/ramen/**": { lines: 92, statements: 92, branches: 88 },
      },
    },
  },
});
```

**`frontend/package.json`** scripts (add three, change nothing else):

```json
"test": "vitest run",
"test:watch": "vitest",
"test:coverage": "vitest run --coverage"
```

`npm test` is the whole command, the same way a bare `pytest` is on the Python
side. Nobody should have to remember flags.

### 2.6 `tests/setup.js`

Mirror `tests/conftest.py`, which "replaces `requests.get` at import time with
one that refuses any non-loopback host." The frontend equivalent:

```js
import "@testing-library/jest-dom/vitest";
import { beforeEach, vi } from "vitest";

// recharts needs it; neither jsdom nor happy-dom ships it.
global.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// No test may reach the network. A test that forgets to stub fetch fails
// loudly here rather than quietly hitting :8000 - or MISO.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      throw new Error("unstubbed fetch - stub it in the test");
    }),
  );
});
```

### 2.7 Layout

```
frontend/tests/
  setup.js                  # <- conftest.py
  fixtures/answers.js       # <- support.py
  MisoRamen.test.jsx
  MapBlock.test.jsx
  ChartBlock.test.jsx
  Markdown.test.jsx
  MisoLandingPage.test.jsx
  App.test.jsx
```

A `tests/` directory rather than colocated `__tests__/`, so "where are the tests"
has one answer in this repo regardless of which lane you are in.

**Naming, non-negotiable (AGENTS.md, *Conventions*):** the file is named for the
module; the `it()` is named for the **behavior it protects**, never for the
function it calls. `it("hands the user MISO's contact form when the backend is
unreachable")`, not `it("handles fetch rejection")`. Where a test exists because
of a bug that actually happened, a one-line comment above it says so - that is
what stops the next person deleting it as redundant.

---

## 3. Coverage budget

### 3.1 The honest denominators

The table below uses *estimated* coverable lines. **Before trusting it, run
`npm run test:coverage` once with zero tests written** - v8 reports the real
denominator per file, and it is the only number worth planning against. Adjust
the per-file targets, not the 90 percent floor.

| File | Lines | Est. coverable | Lines target | Branch target | Why that number |
|---|---:|---:|---:|---:|---|
| `src/ramen/MapBlock.jsx` | 111 | ~75 | **100%** | **95%** | The security-relevant file. Pure function of its props, no async, no third-party renderer. There is no excuse for a gap here. |
| `src/ramen/Markdown.jsx` | 49 | ~30 | **100%** | **100%** | Four component overrides and two regexes. Every branch is one fixture string. |
| `src/ramen/ChartBlock.jsx` | 169 | ~120 | **95%** | **90%** | Four chart types x legend on/off. The last 5% is recharts prop plumbing that jsdom cannot observe without asserting on internals (see §5). |
| `src/ramen/MisoRamen.jsx` | 362 | ~250 | **88%** | **80%** | The pointer-drag resize (`:81-102`) and `autoGrow` (`:112-117`) depend on layout jsdom does not have. Worth ~10 lines of deliberate shortfall rather than a page of fake `getBoundingClientRect`. |
| `src/App.jsx` | 13 | ~8 | **100%** | n/a | One render. |
| `src/ramen/usStates.js` | 6 | ~4 | **100%** | n/a | Three `export const`s; free the moment `MapBlock` imports it. |
| `src/main.jsx` | 9 | - | **excluded** | - | `createRoot(document.getElementById("root"))`. Testing it means building a `#root` to assert that React mounts. It protects nothing. |
| `src/fake-landingpage/MisoLandingPage.jsx` | 123 | ~110 | see §3.3 | n/a | Static markup. See below. |

### 3.2 The arithmetic

Counting `src/ramen/**` + `App.jsx` + `usStates.js`, excluding `main.jsx` and the
landing page:

```
MisoRamen    250 x 88%  = 220
ChartBlock   120 x 95%  = 114
MapBlock      75 x 100% =  75
Markdown      30 x 100% =  30
App            8 x 100% =   8
usStates       4 x 100% =   4
------------------------------
             487           451   = 92.6%
```

That is 2.6 points of headroom over the floor, which is the right amount: enough
that a small refactor does not turn the build red, not so much that a whole
untested feature can slip in under it.

### 3.3 The landing page, honestly

`MisoLandingPage.jsx` is 123 lines of JSX with no props, no state, no handlers,
and no conditionals. AGENTS.md says to keep it that way. One `render()` call
covers essentially all 123 lines - about 20 percent of the entire frontend -
for about 45 seconds of work, and that coverage means **nothing**. It cannot
regress in any way a line counter can see.

Two defensible options. Padding the number is not one of them.

- **Recommended: include it, with one honest smoke test, and fence it.** Write
  `it("renders the backdrop page with its nav landmarks and no interactive
  logic")`. It is a genuine test - it catches an import breaking or someone
  wiring state into a file AGENTS.md says must stay static. Then the per-glob
  `"src/ramen/**"` threshold in §2.5 means the ramen code has to earn its own
  92 percent regardless of what the backdrop contributes.
- **Acceptable: exclude it** with the comment `// 123 lines of static markup; a
  coverage number here would be a true statement answering a question nobody
  asked.` (Same failure mode `pytest.ini` calls out about measuring
  `backend.poller` alone.)

Do not write per-section assertions for the hero, the cards and the footer to
"raise coverage." That is a test suite that only ever fails when someone edits
copy.

---

## 4. Test inventory

~58 tests. Ordered by what would actually hurt if it broke.

### 4.1 `MisoRamen.test.jsx` - the `/ask` contract and every failure path (20)

The contract is `POST /ask {question}` -> `{answer, sources[{title,url}], as_of}`
(AGENTS.md; `frontend/vite.config.js`; `backend/routes/ask.py:54`). This block is
the frontend half of `tests/test_routes.py::test_ask_returns_the_contract_both_uis_depend_on`.

| Test | What it protects / what breaks |
|---|---|
| `it("sends the typed question to /ask as JSON")` | The request shape. Assert method, `Content-Type`, and `JSON.parse(body).question`. If the key is ever renamed, FastAPI 422s every question (`ask.py:18-20`). |
| `it("renders the answer, its source chips and the as-of stamp")` | All three graded product rules in one pass (UI_RULES.md §8). Chips carry `target=_blank rel="noopener noreferrer"` (`MisoRamen.jsx:302-308`). |
| `it("shows the user's question immediately, before the backend answers")` | `MisoRamen.jsx:124` appends the user turn before the await. Regressing this makes the panel look dead for the length of a Claude call. |
| `it("clears the input as soon as a question is sent")` | `:125`. |
| `it("shows the searching indicator while the request is in flight")` | `:323-332`, queried by its aria-label. Loses the only signal that anything is happening. |
| `it("hides the searching indicator once the answer arrives")` | The `finally` at `:159-161`. |
| `it("renders an answer with no sources without a sources row")` | `:299`. `sources: []` is legal; an empty chip strip is not. |
| `it("keeps the as-of stamp off answers that have no as_of")` | `:143` + `:295`. A missing stamp must not render "as of null" - staleness disclosure is a graded rule, and a wrong one is worse than none. |
| `it("hands the user MISO's contact form when the network request rejects")` | The graceful handoff (`:146-158`). Reject the fetch promise. Assert the chip's href is `https://www.misoenergy.org/about/contact-us/`. **Never a dead end.** |
| `it("hands off when the backend rate-limits at 429")` | `resp.ok` false (`:135`). Matches `ask.py:29`. |
| `it("hands off when Claude is not configured and the backend returns 503")` | `ask.py:32` - the path CI's DAST job exercises deliberately. This is the most likely failure a judge sees. |
| `it("hands off when the Claude API fails and the backend returns 502")` | `ask.py:39-42`. |
| `it("hands off when the response body is not valid JSON")` | `resp.json()` rejects at `:136`. A proxy error page returning HTML with a 200 lands here. Distinct from the `!resp.ok` path and covers a different `catch` entry. |
| `it("marks a failed answer so it renders as an error, not as an answer")` | `messageClass` (`:29-37`) + `error: true`. A hallucination-shaped failure that renders like a normal answer is the worst outcome in the product. |
| `it("leaves the panel usable after a failure")` | Input re-enabled, a second question succeeds. One backend hiccup must not end the demo. |
| `it("keeps the input disabled while a question is in flight")` | `:347,:353`. Use a fetch that never resolves; assert disabled, then assert the request count is still 1. |
| `it("refuses to send a second question while one is in flight")` | The `loading` guard at `:122`. Directly protects against double-charging a Claude call. |
| `it("ignores an empty or whitespace-only question")` | `:121-122`. |
| `it("sends on Enter and inserts a newline on Shift+Enter")` | `:171-176`, documented in `frontend/README.md`. |
| `it("submits a suggested question the moment it is clicked")` | `:268`, UI_RULES.md §6. The suggestions are the demo's opening move. |

### 4.2 `MapBlock.test.jsx` - the allowlist (11)

**This is the block that matters.** The prompt is told the footprint
(`backend/llm/prompts.py:52`); `MapBlock.jsx:50-54` is what makes it true. Every
test here should fail if that `.filter()` is deleted.

Query strategy: each `<path>` carries `<title>{name}</title>`, and lit states are
`fill="#087CC1" fill-opacity="1"` while tinted ones are `fill-opacity="0.28"`
(`:67-68`). Write one helper, `litStateNames(container)`, that returns the sorted
names of paths with `fill-opacity="1"`, and assert on that list. Assert on
*names*, not on path `d` strings.

| Test | What it protects / what breaks |
|---|---|
| `it("lights only the states named in the highlight list")` | The happy path: `["AR","LA","MS","TX"]` lights exactly four. |
| `it("refuses to light a state outside MISO's footprint")` | **The one.** `["CA","NY","FL","IN"]` lights Indiana and nothing else. Delete the filter and this fails. |
| `it("refuses to light every state when the model asks for all fifty")` | Pass all 50 codes; assert the lit set equals the 16-code footprint exactly. Catches an allowlist that was widened rather than removed. |
| `it("ignores highlight entries that are not state codes at all")` | `["", "  ", "XX", "US", "javascript:alert(1)", "../../etc/passwd"]` lights nothing and does not throw. `String(c)` at `:52` is the only coercion. |
| `it("ignores non-string highlight entries")` | `[null, 42, {}, ["IN"], true]` lights nothing. `JSON.parse` will hand you these; nothing upstream rejects them. |
| `it("accepts a full state name as well as its code")` | `["Indiana","Louisiana"]` - the documented behavior at `:41-42,:43-49`. |
| `it("accepts codes regardless of case and surrounding whitespace")` | `[" in ", "il"]` - `.trim().toLowerCase()` / `.toUpperCase()` at `:52`. |
| `it("pins MISO's footprint to the sixteen codes the product claims")` | Assert the lit set for an all-codes highlight equals `AR IA IL IN KY LA MI MN MO MS MT ND SD TX WI MB`. **Note the drift:** `MapBlock.jsx:8-11` lists 16; `backend/config.py:19-23` lists 15 - it has no `MB`. Pin both sides so the two lists cannot separate unnoticed. |
| `it("tints the whole footprint even when nothing is highlighted")` | `highlight: []` - the map still shows MISO's territory (UI_RULES.md §8). |
| `it("always says the footprint follows utility boundaries, not state lines")` | The legend note at `:105-107`. It is a factual disclaimer about MISO's territory, not decoration. |
| `it("falls back to the raw block when the map spec is unusable")` | `parseSpec` (`:19-29`): invalid JSON, `null`, a bare string, `{}` with no `highlight`, `highlight` as a string. Each renders `<pre>` with the original text, never a broken map. |

### 4.3 `Markdown.test.jsx` - rendering safety and fence routing (11)

Everything here is rendering **model output**.

| Test | What it protects / what breaks |
|---|---|
| `it("escapes raw HTML in an answer instead of rendering it")` | Feed `<img src=x onerror="...">` and `<script>`. Assert the text is visible and `container.querySelector("img,script")` is null. Adding `rehype-raw` - a one-line "fix" someone will propose - fails this. |
| `it("leaves a javascript: link inert")` | `[click](javascript:alert(1))` - react-markdown v10's `defaultUrlTransform` blanks the href, and the `a` override at `:19-23` receives the sanitized value. Assert `getAttribute("href")` is `""`. Overriding `urlTransform` fails this. |
| `it("leaves a data: link inert")` | Same mechanism, different payload (`data:text/html,...`). |
| `it("keeps ordinary http and https links working and opening in a new tab")` | `:20`. The link **is** the product; the sanitizer must not eat real ones. |
| `it("gives every outbound link rel=noopener noreferrer")` | `:20`. Regressing it hands the opener to a page MISO does not control. |
| `it("routes a chart fence to the chart renderer")` | `:26-28`. |
| `it("routes a map fence to the map renderer")` | `:29-31`. |
| `it("leaves an ordinary code fence as a code block")` | `:32-36`. A ```json block must not be routed anywhere. |
| `it("routes only exact chart and map fences")` | **Known gap:** `:26` and `:30` are substring matches, so ```chartreuse routes to `ChartBlock` and ```mapreduce routes to `MapBlock`. Pin the current behavior with a comment naming it, or tighten to `/^language-(chart|map)$/` and pin the tight behavior. Do not leave it unwritten. |
| `it("renders LaTeX as real math rather than as source text")` | `$\\frac{a}{b}$` produces `.katex` nodes (UI_RULES.md §8: "never plain-text approximations"). Catches `remark-math`/`rehype-katex` falling out of the plugin list. |
| `it("wraps wide tables so they scroll instead of breaking the panel")` | `:39-43`. A GFM table must land inside `.miso-md-tablewrap`; without it a wide table blows out a 360px panel. |

### 4.4 `ChartBlock.test.jsx` - parsing and the caps (13)

See §5 for the recharts mock these depend on.

| Test | What it protects / what breaks |
|---|---|
| `it("falls back to the raw block when the chart spec is not valid JSON")` | `:31-37,:59-65`. Claude truncating mid-JSON is the common real case. |
| `it("falls back when the spec has no labels")` | `:39` - missing, empty, or not an array. |
| `it("falls back when the spec has no series")` | `:40`. |
| `it("falls back when the spec parses to something that is not an object")` | `"null"`, `"42"`, `"\"hi\""` at `:38`. `JSON.parse("null")` is valid JSON and would sail past a `try/catch` alone. |
| `it("draws at most four series no matter how many the model sends")` | `:67`. Send six; assert four rendered series. The palette is locked at four (UI_RULES.md §8) and a fifth would have to cycle a color - which the rules forbid. |
| `it("assigns the locked palette in its fixed order")` | `#087CC1, #B36F0E, #00879E, #3E8814`. Colorblind-validated; reordering is a UI_RULES violation nobody would notice by eye. |
| `it("coerces string values into numbers")` | `:49`. **MISO's APIs return strings, not numbers** (AGENTS.md, *Hard external constraints*). `"1842"` must become `1842`. |
| `it("substitutes zero for a value the model left out")` | `s.data?.[i] ?? 0` at `:49` - short `data` arrays. |
| `it("does not crash on a non-numeric value")` | `Number("n/a")` is `NaN` at `:49`. Pin what happens today, and put the decision in the test name if the team changes it to drop the point instead. |
| `it("survives a series entry that is null")` | **This crashes the app today.** See §9.1. Write it against the intended behavior, mark it `it.fails` with a comment pointing at §9.1, and delete the marker with the fix. |
| `it("renders a line, bar, area or pie chart as the spec asks")` | `:75-159`, four cases, and an unknown `type` falls through to a line chart (`:139-140`). |
| `it("hides the legend for a single series and shows it for several")` | `:72`. The dataviz rule: one series needs no legend, the title names it. |
| `it("renders a very large labels array without capping it")` | **Known gap, no cap exists.** `labels` is model-controlled and used unbounded at `:46` and `:77`. 5,000 labels means 5,000 rows or 5,000 pie slices in a 360px panel. Assert the current behavior explicitly and file §9.2 - a pinned gap is a gap someone can find. |

### 4.5 `MisoRamen.test.jsx`, part two - panel behavior (5)

Lower value, but this is where the last few coverage points live.

| Test | What it protects / what breaks |
|---|---|
| `it("offers the crosswalk CSV only on answers that name the API host")` | `:21,:312-316`. Positive on an answer containing `apim.misoenergy.org`, negative on one that does not. **Note the bug at §9.3.** |
| `it("closes to a launcher and reopens from it")` | `:181-211,:238-247`. If the launcher stops opening, the demo is over. Assert by aria-label. |
| `it("dismisses the greeting bubble after the visitor interacts with the page")` | `:55-77`, with `vi.useFakeTimers()`. Note the comment says three seconds and the timer is 500ms (§9.6) - fix one of them, then write the test against the truth. |
| `it("grows the panel when the grip is dragged up and left")` | `:81-102`. `fireEvent.pointerDown` on the separator, `pointerMove` on window, assert the inline width/height grew. Set `window.innerWidth` via `Object.defineProperty` - assigning it directly does not stick in jsdom. |
| `it("never lets the panel shrink below its minimum or exceed the screen")` | `clamp` (`:105-109`) with `MIN_SIZE` and `SCREEN_MARGIN`. Both bounds, one test. |

### 4.6 `App.test.jsx` and `MisoLandingPage.test.jsx` (2)

| Test | What it protects / what breaks |
|---|---|
| `it("mounts the backdrop page with the chat panel on top of it")` | `App.jsx:6-13`. The composition root. |
| `it("renders the backdrop page with its nav landmarks and no interactive logic")` | The static-by-design rule from AGENTS.md. See §3.3 for why this is the only landing-page test worth writing. |

---

## 5. What NOT to test, and why

A list of everything is not a plan. These are deliberately out of scope:

- **recharts internals.** Do not assert that an SVG `<path>` has the right `d`,
  that axis ticks are positioned correctly, or that a tooltip appears on hover.
  That tests recharts, which has its own suite, and it breaks on their minor
  versions. Test *our* decisions - which chart type, how many series, what
  colors, what numbers - by mocking the recharts module and asserting on the
  props we pass it:

  ```js
  vi.mock("recharts", async () => {
    const stub = (name) => (props) =>
      <div data-testid={name} data-props={JSON.stringify(props.dataKey ?? props.data ?? null)}
           data-fill={props.fill} data-stroke={props.stroke}>{props.children}</div>;
    return { Area: stub("Area"), AreaChart: stub("AreaChart"), Bar: stub("Bar"),
             BarChart: stub("BarChart"), CartesianGrid: stub("CartesianGrid"),
             Cell: stub("Cell"), Legend: stub("Legend"), Line: stub("Line"),
             LineChart: stub("LineChart"), Pie: stub("Pie"), PieChart: stub("PieChart"),
             ResponsiveContainer: stub("ResponsiveContainer"), Tooltip: stub("Tooltip"),
             XAxis: stub("XAxis"), YAxis: stub("YAxis") };
  });
  ```

  This is also the *only* way these tests pass: `ResponsiveContainer` measures
  its parent, and in jsdom every element is 0x0, so the real component renders
  nothing at all.

- **CSS class names as the primary query.** Query by role, label and text.
  `getByLabelText("Send question")` also proves UI_RULES.md §10 (every button has
  an aria-label) - `.miso-ramen-send` proves nothing and breaks on a rename. The
  two justified exceptions are already named above: the `fill-opacity` attribute
  in `MapBlock` (it *is* the behavior - lit versus tinted) and
  `.miso-md-tablewrap` (it *is* the scroll containment).

- **Exact copy.** Do not assert the full sentence of the handoff message
  (`MisoRamen.jsx:153-155`), the intro paragraph, or the suggested-question
  wording. Assert that the contact-form **link** is present. Copy is edited
  often; a suite that goes red on a word change trains people to ignore it.

- **`main.jsx`.** Excluded from coverage. See §3.1.

- **Snapshots.** No `toMatchSnapshot` anywhere. A 300-line snapshot of a chat
  panel gets regenerated on failure without being read, which is a test that
  costs maintenance and protects nothing.

- **Styling and layout.** No assertions on computed colors, widths, radii or
  fonts. jsdom has no layout engine, so any such assertion is measuring the
  inline style string you just wrote. The locked palette is enforced by
  UI_RULES.md and code review - except the chart series palette, which is data
  (`ChartBlock.jsx:25`) and *is* tested.

- **The Streamlit UI.** `app.py` is independent by design (AGENTS.md) and has its
  own lane. Nothing here touches it.

- **End-to-end against a live backend.** No test in this suite may open a socket
  (§2.6). The pr-gate workflow's rule - "nothing in this workflow may ever
  contact MISO" - is not negotiable, and a frontend test that hits `:8000` is one
  proxy config away from being a frontend test that hits MISO.

---

## 6. Fixtures

`tests/fixtures/answers.js` holds realistic `/ask` responses in the real contract
shape. **Draw them from real backend output**, not from imagination - paste a real
answer, then trim it. A fixture that does not look like what Claude actually
emits tests the wrong thing.

Ship one helper alongside them so no test hand-rolls a `Response`:

```js
export const okResponse = (body) => ({ ok: true, status: 200, json: async () => body });
export const errorResponse = (status) => ({ ok: false, status, json: async () => ({ detail: "" }) });
export const badJsonResponse = () => ({ ok: true, status: 200,
  json: async () => { throw new SyntaxError("Unexpected token < in JSON at position 0"); } });
```

### Fixture A - live grid data with a chart

The demo's opening question. Exercises the full contract, the chart fence, bold
markdown, and the as-of stamp.

```js
export const fuelMixAnswer = {
  answer: [
    "MISO is generating about **1,842 MW** of wind right now, roughly 2% of the",
    "footprint's total output. Wind is well below its typical share for this hour.",
    "",
    "```chart",
    '{"type": "bar", "title": "Generation by fuel type", "unit": "MW",',
    ' "labels": ["Coal", "Natural Gas", "Nuclear", "Wind"],',
    ' "series": [{"name": "Output", "data": [31204, 48310, 12770, 1842]}]}',
    "```",
    "",
    "See [Real-Time Displays](https://www.misoenergy.org/markets-and-operations/real-time--market-data/) for the live figures.",
  ].join("\n"),
  sources: [
    { title: "MISO Real-Time Fuel Mix", url: "https://www.misoenergy.org/markets-and-operations/real-time--market-data/" },
  ],
  as_of: "6:55 PM EST",
};
```

### Fixture B - a crosswalk answer, which must offer the CSV

Contains `apim.misoenergy.org`, which is what triggers the download chip
(`MisoRamen.jsx:21`). Also carries a GFM table, so it doubles as the table-wrapper
fixture.

```js
export const crosswalkAnswer = {
  answer: [
    "The **MLC** column from the Real-Time LMP report moved into the Data Exchange",
    "pricing API as a nested field.",
    "",
    "| Old report column | New API field | Endpoint |",
    "|---|---|---|",
    "| LMP | `lmp` | `https://apim.misoenergy.org/pricing/v1/real-time/{date}/lmp` |",
    "| MCC | `congestionComponent` | same |",
    "| MLC | `lossComponent` | same |",
    "",
    "The legacy CSV retires **Sept 30, 2026**.",
  ].join("\n"),
  sources: [
    { title: "MISO Data Exchange - Pricing API", url: "https://apim.misoenergy.org/pricing" },
  ],
  as_of: "6:55 PM EST",
};
```

### Fixture C - a "where" answer with a map and LaTeX

```js
export const footprintAnswer = {
  answer: [
    "MISO's **South region** covers Arkansas, Louisiana, Mississippi and eastern Texas.",
    "It contributes roughly $\\frac{1}{5}$ of the footprint's peak load.",
    "",
    "```map",
    '{"title": "MISO South", "highlight": ["AR", "LA", "MS", "TX"], "label": "MISO South"}',
    "```",
  ].join("\n"),
  sources: [{ title: "MISO Regions", url: "https://www.misoenergy.org/about/miso-footprint/" }],
  as_of: "6:55 PM EST",
};
```

### The adversarial set

Small, named, and kept next to the honest ones. These are the model-output shapes
the renderers have to survive - the point of the suite:

`truncatedChartSpec` (JSON cut mid-object) · `chartWithSixSeries` ·
`chartWithStringValues` (`["1842", "31204"]` - MISO's real wire format) ·
`chartWithNullSeriesEntry` (§9.1) · `chartWithFiveThousandLabels` (§9.2) ·
`mapWithOutOfFootprintStates` (`["CA","NY","FL","IN"]`) ·
`mapWithEveryStateCode` · `mapWithJunkHighlights` (`[null, 42, "", "XX", {}]`) ·
`answerWithRawHtml` · `answerWithJavascriptLink`.

---

## 7. CI

### Where it goes

**A new `frontend-test` job in `.github/workflows/security.yml` (the `pr-gate`
workflow), immediately after `frontend-build`.**

```yaml
  frontend-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
        working-directory: frontend
      # vitest.config.js carries the coverage thresholds, so a drop fails the
      # job the same way a failing test does - the same deal as pytest.ini.
      - run: npm run test:coverage
        working-directory: frontend
```

Reasoning:

- `pr-gate` is the workflow whose jobs are marked as required checks in branch
  protection. A test that does not block a merge is a report, not a gate.
- The Node 22 + npm-cache stanza already exists there in `frontend-build` -
  copy it verbatim rather than inventing a second frontend setup.
- The suite is fast and deterministic and opens no sockets, which is exactly what
  that workflow's header says it is for.

**A separate job, not extra steps bolted onto `frontend-build`.** They run in
parallel, and two failures produce two distinct red checks - "the build broke"
and "a test broke" are different problems for different people.

The counter-argument, and why it loses: `tests.yml` is named `tests` and is the
obvious place to look. But it is shaped entirely around the Python matrix
(`python-version`, `pip install`), and a Node job wedged into it would make its
`3.12 / 3.14` matrix meaningless for half its content. Add a comment in
`tests.yml` pointing at `pr-gate` for the frontend, so neither file lies about
what runs where.

### Does coverage gate the PR? Yes.

`pytest.ini` sets `--cov-fail-under=90` and the tests workflow says so out loud:
"a drop in coverage fails the job the same way a failing test does." The
frontend follows the same rule via `thresholds` in `vitest.config.js`. The number
is enforced by config rather than by a CI flag for the same reason as on the
Python side - `npm test` on a laptop must mean the same thing it means in CI.

### Two other files to touch

- **`.github/workflows/post-merge.yml`** already runs `npm audit --audit-level=high`
  in `frontend/`. The new devDependencies get audited automatically. Nothing to
  change, but expect the first run after this lands to have more to say.
- **`frontend/README.md` and `AGENTS.md`.** AGENTS.md currently says "Nothing else
  is covered yet" and the definition of done says `pytest` matters only "if you
  touched `backend/poller/`." Both become false the moment this lands. AGENTS.md
  has already been bitten twice by exactly this - "a document that contradicts
  the code is worse than no document, because it is believed." Update both in the
  same PR. Add to the definition of done: *`npm test` passes, if you touched
  `frontend/src/`.*

---

## 8. Effort and order of work

About **12-14 hours**, or two focused days with the tooling risk in §2.3
unresolved. Ordered so that stopping halfway leaves the valuable half done.

| # | Phase | Est. | Coverage after | Stop here and you have... |
|---|---|---:|---:|---|
| 0 | Tooling: install, `vitest.config.js`, `setup.js`, one trivial `App` test that proves the harness works. Resolve the Vite 8 peer question first. | 1.5 h | ~5% | A working runner. If §2.3 goes badly, you have burned 90 minutes and learned it early. |
| 1 | `MisoRamen` - the `/ask` contract and all six failure paths (§4.1, first 15). | 3 h | ~45% | **The contract is protected.** The single highest-value block. |
| 2 | `MapBlock` - the allowlist, all 11 (§4.2). | 1.5 h | ~60% | **The security test exists.** *This is the natural stopping point.* Phases 1 and 2 are ~6 hours and cover both reasons this document exists. |
| 3 | `Markdown` - HTML escaping, inert links, fence routing (§4.3). | 1.5 h | ~68% | The third rendering-safety block done. |
| 4 | `ChartBlock` - the recharts mock plus all 13 (§4.4). The mock in §5 is most of the work; the tests are quick once it exists. | 2.5 h | ~85% | Parse failures and the caps pinned. |
| 5 | The `MisoRamen` tail - crosswalk chip, launcher, greeting timers, resize, clamp (§4.5). | 2 h | ~91% | Over the floor. |
| 6 | Landing page + `App` smoke tests; run coverage, replace the estimates in §3.1 with real numbers, tune thresholds to leave ~2 points of headroom. | 1 h | ~93% | A gate that is real. |
| 7 | CI job, `AGENTS.md` and `frontend/README.md` updates (§7). | 1 h | - | Done, per the definition of done. |

The fixes in §9 are **not** in this estimate; they are source changes, they are
small, and §9.1 in particular should not wait for a test suite to justify it.

---

## 9. Bugs and gaps found while writing this

Reading six files closely turned up six things. Three are worth fixing before
the suite exists; three are worth pinning with a test and a comment. None of
them are hypothetical.

### 9.1 A valid chart spec crashes the entire chat panel

`ChartBlock.jsx:45-52`:

```js
for (const s of series) {
  row[s.name] = Number(s.data?.[i] ?? 0);
}
```

`parseSpec` (`:31-42`) checks that `series` is a non-empty array. It does not
check what is *in* it. `{"labels":["a"],"series":[null]}` is valid JSON, passes
`parseSpec`, and throws `TypeError: Cannot read properties of null` at `:49` -
and again at `:79` on the pie path. There is no error boundary anywhere in the
tree (`MisoRamen.jsx:291` renders `<Markdown>` bare), so a React render error
unmounts the whole panel. **On stage, the chat window would vanish mid-answer.**

Two-line fix in `parseSpec`:

```js
if (!cfg.series.every((s) => s && typeof s === "object")) return null;
```

...and the existing fallback at `:59-65` shows the raw block instead. Cheaper and
more predictable than an error boundary, and it reuses the failure path the file
already has. An error boundary around `<Markdown>` is still worth having as
defense in depth, but it is a bigger change than the night before a demo allows.

### 9.2 `labels` has no cap, and it is model-controlled

`ChartBlock.jsx:46` and `:77` map over `cfg.labels` unbounded. `series` is capped
at four (`:67`); `labels` is capped at nothing. A model that emits 5,000 labels
gets 5,000 rows or 5,000 pie slices rendered into a 360px panel. Not a security
issue - the model is Claude, not a hostile party - but it is a hang, and hangs on
stage look identical to crashes. Suggested: cap at ~60 with a visible "showing
the first 60 of N" note, or fall back to the raw block above some threshold.
Until then, §4.4's last test pins the gap so it is findable.

### 9.3 The crosswalk CSV chip is unreachable when an answer has no sources

`MisoRamen.jsx:299` gates the entire sources block on
`msg.sources && msg.sources.length > 0`, and the download chip lives *inside* it
at `:312-316`. If a crosswalk answer ever comes back with an empty `sources`
array, the CSV link - the only route to `GET /crosswalk.csv` in the UI -
disappears. The fix is to lift the chip out of that conditional, or to widen the
condition to `sources.length > 0 || isCrosswalkAnswer`.

### 9.4 Fence routing is a substring match

`Markdown.jsx:26` tests `/language-chart/` and `:30` tests `/language-map/`.
A ```chartreuse block routes to `ChartBlock`; a ```mapreduce block routes to
`MapBlock`. Both then fail `parseSpec` and render as `<pre>`, so the visible
damage today is nil - but the intent is clearly an exact match. Tighten to
`/^language-(chart|map)$/` and the tests in §4.3 pin it.

### 9.5 The footprint list exists twice and the copies already differ

`MapBlock.jsx:8-11` lists 16 codes including `MB`. `backend/config.py:19-23`
lists 15 across three regions and has no `MB`. That is defensible - Manitoba is
drawn, not a US state, and the backend groups by region - but nothing anywhere
says so, and nothing catches the two lists drifting further. The §4.2 test pins
the frontend list literally; add the matching assertion on the Python side and a
one-line comment in each file saying the other exists.

### 9.6 A comment that contradicts its own code

`MisoRamen.jsx:61-66`: the comment says "Wait 3 seconds before hiding the
dialogue"; the timeout is `500`. One of the two is wrong. Fix whichever is wrong
before writing §4.5's timer test, so the test does not enshrine the mistake.
