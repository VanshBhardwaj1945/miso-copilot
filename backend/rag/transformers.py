"""Each endpoint's raw JSON -> one plain-English snapshot paragraph (+ as-of, source URL)."""

from typing import Any

# old "real-time-displays" page 404s now; MISO moved it here (checked 2026-09-05)
MISO_DISPLAY_URL = (
    "https://www.misoenergy.org/markets-and-operations/"
    "real-time--market-data/markets-displays/"
)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely parse MISO string numbers (e.g. '1,000' or '$10.00' or '100.00')."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).replace(",", "").replace("$", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return default


def transform_fuelmix(data: dict) -> tuple[str, str, str]:
    """
    Parses FuelMix.json:
    {"RefId": "...", "TotalMW": "1000", "Fuel": {"Type": [{"CATEGORY": "Coal", "ACT": "600"}, ...]}}
    Returns: (prose, as_of_timestamp, source_url)
    """
    ref_id = data.get("RefId", "Recent Interval")
    total_mw = _safe_float(data.get("TotalMW", 0))
    fuels = data.get("Fuel", {}).get("Type", [])

    lines = [
        f"MISO Real-Time Generation Fuel Mix (as of {ref_id}):",
        f"- Total Grid Generation: {total_mw:,.0f} MW",
    ]

    for item in fuels:
        # CATEGORY is the clean name; FUEL_CATEGORY has the MW baked into the text
        category = item.get("CATEGORY") or item.get("FUEL_CATEGORY") or "Unknown"
        act_mw = _safe_float(item.get("ACT", 0))
        pct = (act_mw / total_mw * 100.0) if total_mw > 0 else 0.0
        lines.append(f"- {category}: {act_mw:,.0f} MW ({pct:.1f}%)")

    lines.append(f"Source: MISO Fuel Mix Feed ({MISO_DISPLAY_URL})")
    return "\n".join(lines), ref_id, MISO_DISPLAY_URL


def transform_load(data: dict) -> tuple[str, str, str]:
    """
    Parses RealTimeTotalLoad.json:
    {"LoadInfo": {"RefId": "...", "FiveMinTotalLoad": [{"Load": {"Time": "00:10", "Value": "1020"}}], ...}}
    Returns: (prose, as_of_timestamp, source_url)
    """
    load_info = data.get("LoadInfo", {})
    ref_id = load_info.get("RefId", "Recent Interval")
    five_min = load_info.get("FiveMinTotalLoad", [])
    forecast = load_info.get("MediumTermLoadForecast", [])

    lines = [f"MISO Real-Time Total Electricity Load and Demand (as of {ref_id}):"]

    if five_min:
        latest = five_min[-1].get("Load", {})
        val = _safe_float(latest.get("Value", 0))
        time_str = latest.get("Time", "")
        lines.append(f"- Current 5-Minute Actual Load: {val:,.0f} MW (interval {time_str})")

    if forecast:
        next_fc = forecast[0].get("Forecast", {})
        fc_val = _safe_float(next_fc.get("LoadForecast", 0))
        he = next_fc.get("HourEnding", "")
        lines.append(f"- Day-Ahead Forecasted Load: {fc_val:,.0f} MW (Hour Ending {he})")

    lines.append(f"Source: MISO Real-Time Total Load ({MISO_DISPLAY_URL})")
    return "\n".join(lines), ref_id, MISO_DISPLAY_URL


def transform_snapshot(data: list) -> tuple[str, str, str]:
    """
    Parses Snapshot.json:
    [{"t": "Current Demand (MW)", "v": "1,000", "d": "1/01/1970 12:00:00 AM EST"}, ...]
    Returns: (prose, as_of_timestamp, source_url)
    """
    as_of = data[0].get("d", "Recent Interval") if data else "Recent Interval"
    lines = [f"MISO System Real-Time Snapshot Overview (as of {as_of}):"]

    for row in data:
        metric = row.get("t", "Metric")
        val = row.get("v", "N/A")
        lines.append(f"- {metric}: {val}")

    lines.append(f"Source: MISO Real-Time Snapshot ({MISO_DISPLAY_URL})")
    return "\n".join(lines), as_of, MISO_DISPLAY_URL


def transform_windsolar(data: dict) -> tuple[str, str, str]:
    """
    Parses WindSolar.json:
    {"instance": [{"ActualDateTimeEST": "...", "ActualWindValue": "90.00", ...}], "RefId": "..."}
    Returns: (prose, as_of_timestamp, source_url)
    """
    ref_id = data.get("RefId", "Recent Interval")
    instances = data.get("instance", [])

    lines = [f"MISO Renewable Generation - Wind and Solar (as of {ref_id}):"]

    # Locate latest row with non-null actuals
    actuals = [
        r for r in instances
        if r.get("ActualDateTimeEST") is not None and r.get("ActualWindValue") is not None
    ]
    if actuals:
        latest = actuals[-1]
        w_val = _safe_float(latest.get("ActualWindValue", 0))
        s_val = _safe_float(latest.get("ActualSolarValue", 0))
        ts = latest.get("ActualDateTimeEST", "")
        lines.append(f"- Latest Actual Wind Output: {w_val:,.1f} MW (recorded {ts})")
        lines.append(f"- Latest Actual Solar Output: {s_val:,.1f} MW (recorded {ts})")

    if instances:
        fc = instances[0]
        fc_w = _safe_float(fc.get("ForecastWindValue", 0))
        fc_s = _safe_float(fc.get("ForecastSolarValue", 0))
        lines.append(f"- Day-Ahead Forecasted Wind: {fc_w:,.1f} MW")
        lines.append(f"- Day-Ahead Forecasted Solar: {fc_s:,.1f} MW")

    lines.append(f"Source: MISO Wind & Solar Report ({MISO_DISPLAY_URL})")
    return "\n".join(lines), ref_id, MISO_DISPLAY_URL

# --- MISO Data Exchange -----------------------------------------------------

# The API replacing the CSV market reports on 2026-09-30. Same idea as the
# legacy feeds above - JSON in, one plain-English paragraph out - but the rows
# are broken out by region, which the display feeds never were.
DATA_EXCHANGE_DOC_URL = (
    "https://data-exchange.misoenergy.org/api-details"
    "#api=load-generation-and-interchange-api"
    "&operation=get-v1-real-time-date-generation-fuel-type"
)

# MISO returns NORTH/CENTRAL/SOUTH/MISO/NO_REGION; these read better in prose.
_REGION_NAMES = {
    "NORTH": "MISO North", "CENTRAL": "MISO Central", "SOUTH": "MISO South",
    "MISO": "MISO overall", "NO_REGION": "unassigned to a region",
}
_FUEL_NAMES = {
    "coal": "coal", "gas": "natural gas", "nuclear": "nuclear",
    "water": "hydro", "wind": "wind", "solar": "solar",
    "storage": "storage", "other": "other",
}


def _row_time(row: dict) -> str:
    """The row's interval, as a sortable string. Missing sorts oldest."""
    interval = row.get("timeInterval") or {}
    return str(interval.get("start") or interval.get("value") or "")


def _latest_row_per_region(rows: list) -> dict:
    """The newest row for each region.

    A day's fetch holds every interval and only the newest is "right now", so
    this compares timeInterval rather than trusting the order rows arrive in.
    MISO does not document that ordering anywhere, and taking the last row on
    faith would silently report the oldest interval of the day as current if
    the API ever returned newest-first.
    """
    latest: dict = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("region"):
            continue
        code = str(row["region"]).upper()
        if code not in latest or _row_time(row) >= _row_time(latest[code]):
            latest[code] = row
    return latest


def transform_de_fueltype(data: dict) -> tuple[str, str, str]:
    """Data Exchange real-time generation by fuel type, per region.

    Returns: (prose, as_of_timestamp, source_url)
    """
    rows = data.get("data") if isinstance(data, dict) else None
    latest = _latest_row_per_region(rows or [])
    if not latest:
        return ("No MISO Data Exchange generation data is available.", "", DATA_EXCHANGE_DOC_URL)

    # every row of one fetch shares an interval; take it from any of them
    sample = next(iter(latest.values()))
    interval = sample.get("timeInterval") or {}
    # "value" is the hour number ("1".."24"), not a timestamp - reading it as
    # one produced an as-of of "24". "start" is the ISO interval start.
    as_of = str(interval.get("start") or interval.get("end") or "").strip()

    # These endpoints publish a completed market day at 2am EST the day after,
    # so this is never "right now" however the /real-time/ path reads. Saying so
    # is the same duty as the as-of stamp: staleness stays visible.
    lines = [f"MISO generation by fuel type and region for the completed market "
             f"day, from the MISO Data Exchange API (interval starting "
             f"{as_of} EST). This is a settled market day, not live output - "
             f"for current generation use the real-time fuel mix." if as_of else
             "MISO generation by fuel type and region for a completed market "
             "day, from the MISO Data Exchange API."]

    # MISO first when present - it is the footprint total the others sum toward.
    # Anything MISO adds to the enum later is appended rather than dropped: a
    # region silently missing from an answer is worse than one with an
    # unpolished name.
    known = ("MISO", "NORTH", "CENTRAL", "SOUTH", "NO_REGION")
    order = [r for r in known if r in latest] + sorted(set(latest) - set(known))
    for code in order:
        row = latest[code]
        total = _safe_float(row.get("totalMw"))
        fuels = row.get("fuelTypes") or {}
        parts = []
        for key, label in _FUEL_NAMES.items():
            mw = _safe_float(fuels.get(key))
            if mw <= 0:
                continue
            share = f" ({mw / total * 100:.1f}%)" if total > 0 else ""
            parts.append(f"{label} {mw:,.0f} MW{share}")
        name = _REGION_NAMES.get(code, code)
        if parts:
            lines.append(f"- {name}: {total:,.0f} MW total - " + ", ".join(parts) + ".")
        else:
            lines.append(f"- {name}: {total:,.0f} MW total.")

    # Only warn about the footprint total when one is actually present: MISO
    # returns the three regions and no MISO-wide row, so the warning would
    # otherwise describe data that is not there.
    if "MISO" in latest:
        lines.append("\"MISO overall\" is the whole footprint, so do not add it "
                     "to the three regions.")
    else:
        lines.append("These are MISO's three regions - North, Central and South. "
                     "Their sum is the footprint total.")
    return "\n".join(lines), as_of, DATA_EXCHANGE_DOC_URL


# --- the rest of the region-capable Data Exchange endpoints -----------------

# Every one of these returns rows of {timeInterval, region, ...values}, but the
# value fields differ per endpoint. Rather than eleven near-identical
# transformers, one generic one - which means it has to know what each field
# actually IS. A count or a boolean printed as megawatts is a confidently wrong
# number, which is worse than a missing one.
#
# label, unit. An empty unit means the number is not a measurement in MW.
_MEASURES = {
    "load": ("load", "MW"),
    "nsi": ("net scheduled interchange", "MW"),
    "supply": ("cleared supply", "MW"),
    "fixed": ("fixed demand", "MW"),
    "priceSens": ("price-sensitive demand", "MW"),
    "virtual": ("virtual demand", "MW"),
    "mustRun": ("must-run", "MW"),
    "economic": ("economic", "MW"),
    "emergency": ("emergency", "MW"),
    "loadForecast": ("load forecast", "MW"),
    "totalMw": ("total", "MW"),
    "unitCount": ("units", ""),          # a count of generators, not megawatts
}

# Fields that say which slice a row describes rather than how much of anything.
# They become part of the row's label; they are never printed as quantities.
_DIMENSIONS = ("fuelType", "localResourceZone")

# Row bookkeeping with no meaning in an answer.
_IGNORED = {"region", "interval", "init", "timeInterval", "peak"}


def _measures(row: dict) -> str:
    """The row's actual measurements, as "label 1,234 MW" phrases.

    Only fields in _MEASURES are reported. An unknown field is skipped rather
    than guessed at: inventing a unit for it is how "units 2 MW" happened.
    """
    parts = []
    for key, value in row.items():
        if key in _IGNORED or key in _DIMENSIONS:
            continue
        if key == "fuelTypes" and isinstance(value, dict):
            for fuel, mw in value.items():
                amount = _safe_float(mw)
                if amount > 0:
                    parts.append(f"{_FUEL_NAMES.get(fuel, fuel)} {amount:,.0f} MW")
            continue
        if isinstance(value, bool) or key not in _MEASURES:
            continue
        # null is "not reported", not zero. 7 of 96 rows in a real day-ahead
        # demand payload are null, and "price-sensitive demand 0 MW" is a
        # number MISO never published.
        if value is None:
            continue
        label, unit = _MEASURES[key]
        amount = _safe_float(value)
        parts.append(f"{label} {amount:,.0f} {unit}".rstrip())
    return ", ".join(parts)


def _dimension_label(row: dict) -> str:
    """What distinguishes this row from its siblings in the same region."""
    return ", ".join(str(row[d]) for d in _DIMENSIONS if row.get(d))


def _rows_by_region_at_latest_interval(rows: list) -> dict:
    """Every row for each region at that region's newest interval.

    Not one row per region: fuel-on-the-margin carries a row per fuel, and
    keeping only one silently answered "which fuel is on the margin?" with a
    single arbitrary fuel.
    """
    newest: dict = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("region"):
            continue
        code = str(row["region"]).upper()
        when = _row_time(row)
        if code not in newest or when > newest[code][0]:
            newest[code] = (when, [])
        if when == newest[code][0]:
            newest[code][1].append(row)
    return newest


def make_de_transformer(title: str, doc_url: str):
    """A transformer for one region-scoped Data Exchange endpoint.

    Same contract as every other transformer: (data) -> (prose, as_of, url).
    Reports the newest interval for each region, because a day's fetch holds
    every interval and only the newest is the end state of that market day.
    """
    def transform(data: dict) -> tuple[str, str, str]:
        rows = data.get("data") if isinstance(data, dict) else None
        latest = _rows_by_region_at_latest_interval(rows or [])
        if not latest:
            return (f"No MISO Data Exchange data is available for {title}.", "", doc_url)

        as_of = max((when for when, _ in latest.values()), default="")

        lines = [f"MISO {title} by region for the completed market day, from the "
                 f"MISO Data Exchange API"
                 + (f" (interval starting {as_of} EST)" if as_of else "")
                 + ". This is a settled market day, not live output."]
        known = ("MISO", "NORTH", "CENTRAL", "SOUTH", "NO_REGION")
        order = [r for r in known if r in latest] + sorted(set(latest) - set(known))
        for code in order:
            _, rows_here = latest[code]
            name = _REGION_NAMES.get(code, code)
            described = []
            for row in rows_here:
                measures = _measures(row)
                dimension = _dimension_label(row)
                if dimension and measures:
                    described.append(f"{dimension} ({measures})")
                elif dimension:
                    described.append(dimension)
                elif measures:
                    described.append(measures)
            lines.append(f"- {name}: {'; '.join(described)}." if described
                         else f"- {name}: no values reported.")
        return "\n".join(lines), as_of, doc_url
    return transform
