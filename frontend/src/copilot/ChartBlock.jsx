import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// Renders a ```chart fenced block from a backend answer. Expected spec:
// {"type":"line|bar|area|pie","title":"...","unit":"MW",
//  "labels":["..."],"series":[{"name":"...","data":[...]}]}

// Series palette - fixed order, colorblind-validated (see UI_RULES.md).
// Never reorder or cycle; the backend caps answers at 4 series.
const COLORS = ["#087CC1", "#B36F0E", "#00879E", "#3E8814"];

const AXIS_STYLE = { fontSize: 11, fill: "#718392" };
const GRID_COLOR = "#e5ebef";

// Parse the spec JSON; return null if it isn't a usable chart config.
function parseSpec(spec) {
  let cfg;
  try {
    cfg = JSON.parse(spec);
  } catch {
    return null;
  }
  if (!cfg || typeof cfg !== "object") return null;
  if (!Array.isArray(cfg.labels) || cfg.labels.length === 0) return null;
  if (!Array.isArray(cfg.series) || cfg.series.length === 0) return null;
  return cfg;
}

// Reshape {labels, series} into recharts rows: one object per label.
function buildRows(cfg, series) {
  return cfg.labels.map((label, i) => {
    const row = { name: String(label) };
    for (const s of series) {
      row[s.name] = Number(s.data?.[i] ?? 0);
    }
    return row;
  });
}

export default function ChartBlock({ spec }) {
  const cfg = parseSpec(spec);

  // Bad spec: show the raw block instead of a broken chart.
  if (cfg === null) {
    return (
      <pre className="miso-md-badchart">
        <code>{spec}</code>
      </pre>
    );
  }

  const series = cfg.series.slice(0, COLORS.length);
  const rows = buildRows(cfg, series);
  const unit = cfg.unit ? ` ${cfg.unit}` : "";
  const formatValue = (v) => `${Number(v).toLocaleString()}${unit}`;
  // One series needs no legend - the title names it (dataviz rule).
  const showLegend = series.length > 1;

  let chart;
  if (cfg.type === "pie") {
    // Pie uses the first series only: one slice per label.
    const pieData = cfg.labels.map((label, i) => ({
      name: String(label),
      value: Number(series[0].data?.[i] ?? 0),
    }));
    chart = (
      <PieChart>
        <Pie
          data={pieData}
          dataKey="value"
          nameKey="name"
          innerRadius="45%"
          outerRadius="80%"
          stroke="#ffffff"
          strokeWidth={2}
        >
          {pieData.map((_, i) => (
            <Cell key={i} fill={COLORS[i % COLORS.length]} />
          ))}
        </Pie>
        <Tooltip formatter={formatValue} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
      </PieChart>
    );
  } else if (cfg.type === "bar") {
    chart = (
      <BarChart data={rows}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="name" tick={AXIS_STYLE} tickLine={false} />
        <YAxis tick={AXIS_STYLE} tickLine={false} axisLine={false} width={44} />
        <Tooltip formatter={formatValue} />
        {showLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {series.map((s, i) => (
          <Bar
            key={s.name}
            dataKey={s.name}
            fill={COLORS[i]}
            radius={[4, 4, 0, 0]}
            maxBarSize={36}
          />
        ))}
      </BarChart>
    );
  } else if (cfg.type === "area") {
    chart = (
      <AreaChart data={rows}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="name" tick={AXIS_STYLE} tickLine={false} />
        <YAxis tick={AXIS_STYLE} tickLine={false} axisLine={false} width={44} />
        <Tooltip formatter={formatValue} />
        {showLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {series.map((s, i) => (
          <Area
            key={s.name}
            dataKey={s.name}
            stroke={COLORS[i]}
            fill={COLORS[i]}
            fillOpacity={0.18}
            strokeWidth={2}
          />
        ))}
      </AreaChart>
    );
  } else {
    // Default: line chart (also covers an unknown "type").
    chart = (
      <LineChart data={rows}>
        <CartesianGrid stroke={GRID_COLOR} vertical={false} />
        <XAxis dataKey="name" tick={AXIS_STYLE} tickLine={false} />
        <YAxis tick={AXIS_STYLE} tickLine={false} axisLine={false} width={44} />
        <Tooltip formatter={formatValue} />
        {showLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {series.map((s, i) => (
          <Line
            key={s.name}
            dataKey={s.name}
            stroke={COLORS[i]}
            strokeWidth={2}
            dot={{ r: 2.5 }}
          />
        ))}
      </LineChart>
    );
  }

  return (
    <div className="miso-chart">
      {cfg.title && <div className="miso-chart-title">{cfg.title}</div>}
      <ResponsiveContainer width="100%" height={220}>
        {chart}
      </ResponsiveContainer>
    </div>
  );
}
