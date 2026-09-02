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

// Renders a ```chart fenced block from the backend. Spec:
// {"type":"line|bar|area|pie","title":"...","unit":"MW",
//  "labels":["..."],"series":[{"name":"...","data":[...]}]}

// Categorical palette - fixed order, colorblind-validated. Do not reorder
// or cycle; a 5th series folds into "Other" upstream.
const COLORS = ["#087CC1", "#B36F0E", "#00879E", "#3E8814"];

const AXIS = { fontSize: 11, fill: "#718392" };
const GRID = "#e5ebef";

export default function ChartBlock({ spec }) {
  let cfg;
  try {
    cfg = JSON.parse(spec);
    if (!Array.isArray(cfg.labels) || !Array.isArray(cfg.series)) throw 0;
  } catch {
    return (
      <pre className="miso-md-badchart">
        <code>{spec}</code>
      </pre>
    );
  }

  const series = cfg.series.slice(0, 4);
  const rows = cfg.labels.map((label, i) => {
    const row = { name: String(label) };
    for (const s of series) row[s.name] = Number(s.data?.[i]);
    return row;
  });
  const unit = cfg.unit ? ` ${cfg.unit}` : "";
  const fmt = (v) => `${Number(v).toLocaleString()}${unit}`;
  const showLegend = series.length > 1;

  let chart;
  if (cfg.type === "pie") {
    const pieData = cfg.labels.map((label, i) => ({
      name: String(label),
      value: Number(series[0]?.data?.[i]),
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
        <Tooltip formatter={fmt} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
      </PieChart>
    );
  } else if (cfg.type === "bar") {
    chart = (
      <BarChart data={rows}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="name" tick={AXIS} tickLine={false} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={44} />
        <Tooltip formatter={fmt} />
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
  } else {
    const Wrap = cfg.type === "area" ? AreaChart : LineChart;
    chart = (
      <Wrap data={rows}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey="name" tick={AXIS} tickLine={false} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={44} />
        <Tooltip formatter={fmt} />
        {showLegend && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {series.map((s, i) =>
          cfg.type === "area" ? (
            <Area
              key={s.name}
              dataKey={s.name}
              stroke={COLORS[i]}
              fill={COLORS[i]}
              fillOpacity={0.18}
              strokeWidth={2}
            />
          ) : (
            <Line
              key={s.name}
              dataKey={s.name}
              stroke={COLORS[i]}
              strokeWidth={2}
              dot={{ r: 2.5 }}
            />
          )
        )}
      </Wrap>
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
