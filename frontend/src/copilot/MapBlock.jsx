import { US_STATES, VIEW_BOX } from "./usStates.js";

// Renders a ```map fenced block from a backend answer. Expected spec:
// {"title":"...","highlight":["IN","IL",...],"label":"what the highlight means"}
// Every MISO state is tinted; the highlighted subset is solid. The footprint
// list lives in backend/config.py and the prompt - this file only draws.

const FOOTPRINT = ["AR", "IA", "IL", "IN", "KY", "LA", "MI", "MN", "MO", "MS", "MT", "ND", "SD", "TX", "WI"];

const FILL_HIGHLIGHT = "#087CC1";   // Primary MISO Blue
const FILL_FOOTPRINT = "#087CC1";   // same hue, tinted via opacity below
const FILL_OTHER = "#F6F9FB";
const STROKE = "#DCE5EB";

// Parse the spec JSON; return null if it isn't a usable map config.
function parseSpec(spec) {
  let cfg;
  try {
    cfg = JSON.parse(spec);
  } catch {
    return null;
  }
  if (!cfg || typeof cfg !== "object") return null;
  if (!Array.isArray(cfg.highlight)) return null;
  return cfg;
}

export default function MapBlock({ spec }) {
  const cfg = parseSpec(spec);
  if (cfg === null) {
    return (
      <pre className="miso-md-badchart">
        <code>{spec}</code>
      </pre>
    );
  }

  // only real, in-footprint codes get highlighted - the prompt is told the
  // list, but the drawing never trusts it
  const highlight = new Set(
    cfg.highlight.map((c) => String(c).toUpperCase()).filter((c) => FOOTPRINT.includes(c)),
  );

  return (
    <div className="miso-map">
      {cfg.title && <div className="miso-chart-title">{cfg.title}</div>}
      <svg viewBox={VIEW_BOX} role="img" aria-label={cfg.title || "Map of MISO's footprint"}>
        {Object.entries(US_STATES).map(([code, { name, d }]) => {
          const inMiso = FOOTPRINT.includes(code);
          const lit = highlight.has(code);
          return (
            <path
              key={code}
              d={d}
              fill={lit ? FILL_HIGHLIGHT : inMiso ? FILL_FOOTPRINT : FILL_OTHER}
              fillOpacity={lit ? 1 : inMiso ? 0.28 : 1}
              stroke={STROKE}
              strokeWidth={1}
            >
              <title>{name}{inMiso ? " - MISO" : ""}</title>
            </path>
          );
        })}
      </svg>
      <div className="miso-map-legend">
        {highlight.size > 0 && (
          <span>
            <i className="miso-map-swatch miso-map-swatch-lit" /> {cfg.label || "highlighted"}
          </span>
        )}
        <span>
          <i className="miso-map-swatch miso-map-swatch-miso" /> MISO footprint
        </span>
        <span className="miso-map-note">
          MISO serves all or part of each tinted state, plus Manitoba, Canada.
        </span>
      </div>
    </div>
  );
}
