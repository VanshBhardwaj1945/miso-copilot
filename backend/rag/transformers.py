"""Each endpoint's raw JSON -> one plain-English snapshot paragraph (+ as-of, source URL)."""

from datetime import datetime
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


SNAPSHOT_STAMP = "%m/%d/%Y %I:%M:%S %p EST"


def _newest_stamp(rows: list) -> str:
    """The newest timestamp on the snapshot rows, as MISO wrote it.

    Not row zero. Row zero is "Forecasted Peak Demand", stamped midnight, so
    an answer about 8pm demand was labeled "12:00:00 AM" - harmless while this
    value was discarded, wrong on screen now that it reaches the response.
    """
    newest, stamp = None, ""
    for row in rows or []:
        raw = (row.get("d") or "").strip()
        try:
            when = datetime.strptime(raw, SNAPSHOT_STAMP)
        except ValueError:
            continue
        if newest is None or when > newest:
            newest, stamp = when, raw
    if stamp:
        return stamp
    # nothing parsed: MISO changed the format, so say the least we can defend
    return (rows[0].get("d") or "Recent Interval") if rows else "Recent Interval"


def transform_snapshot(data: list) -> tuple[str, str, str]:
    """
    Parses Snapshot.json:
    [{"t": "Current Demand (MW)", "v": "1,000", "d": "1/01/1970 12:00:00 AM EST"}, ...]
    Returns: (prose, as_of_timestamp, source_url)
    """
    as_of = _newest_stamp(data)
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
    """The row's interval start, as a sortable ISO string. Missing sorts oldest.

    `value` is only a fallback and only when it looks like a timestamp: on the
    hourly feeds it is the hour number ("1".."24"), and comparing those as
    strings puts "9" after "24".
    """
    interval = row.get("timeInterval") or {}
    start = str(interval.get("start") or "").strip()
    if start:
        return start
    fallback = str(interval.get("value") or "").strip()
    return fallback if "T" in fallback else ""


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
    dated = f" {as_of[:10]}" if "T" in as_of else ""
    lines = [f"MISO generation by fuel type and region for the completed market "
             f"day{dated}, from the MISO Data Exchange API. This is a settled "
             f"market day, not live output - for current generation use the "
             f"real-time fuel mix. The fuel breakdown below is each region's "
             f"final interval; the day's total range follows it."]
    by_region = _rows_by_region(rows or [])

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
        clock = _clock(_row_time(row))
        ending = f" ending {clock} EST" if clock else ""
        if parts:
            line = f"- {name}: {total:,.0f} MW total{ending} - " + ", ".join(parts) + "."
        else:
            line = f"- {name}: {total:,.0f} MW total{ending}."
        # the same reason every other settled feed carries a range: reporting
        # only the last interval understated MISO Central's generation by 30%
        day = _day_sentence(_day_range(by_region.get(code, [])))
        if day:
            line += f" Across the day, {day}."
        lines.append(line)

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


def _clock(when: str) -> str:
    """The HH:MM of an ISO interval start, for prose. Empty if unparseable."""
    return when[11:16] if "T" in when and len(when) >= 16 else ""


def _row_measures(row: dict) -> dict:
    """Every numeric measure on one row, keyed by measure name.

    fuelTypes is a per-fuel breakdown rather than a measure, and the row
    already carries its sum as totalMw, so it is left out of the arithmetic.
    """
    found = {}
    for key, value in row.items():
        if key in _IGNORED or key in _DIMENSIONS or key == "fuelTypes":
            continue
        if isinstance(value, bool) or value is None or key not in _MEASURES:
            continue
        found[key] = _safe_float(value)
    return found


def _day_range(rows_here: list) -> dict:
    """Per measure, the day's high and low for one region, and when each was.

    Summed across the rows sharing an interval, because a dimensional feed
    splits one region's interval over several rows - fuel-on-the-margin by
    fuel, load forecast by zone - and that region's number for the interval is
    the total, not whichever row happens to come first.

    This exists because reporting only the final interval answered "what was
    MISO Central's load yesterday?" with 41,040 MW, an overnight trough, when
    the day peaked at 54,527 MW. The peak was in the payload and in no
    document.
    """
    totals: dict = {}
    for row in rows_here:
        when = _row_time(row)
        if not when:
            continue
        for key, amount in _row_measures(row).items():
            bucket = totals.setdefault(key, {})
            bucket[when] = bucket.get(when, 0.0) + amount
    ranged = {}
    for key, by_interval in totals.items():
        if len(by_interval) < 2:
            continue            # a single interval is not a range worth stating
        high = max(by_interval.items(), key=lambda kv: kv[1])
        low = min(by_interval.items(), key=lambda kv: kv[1])
        if high[1] == low[1]:
            continue            # flat all day: "peaked at 1 and bottomed at 1"
        ranged[key] = (high, low)
    return ranged


def _day_sentence(ranged: dict) -> str:
    """"load peaked at 54,527 MW (16:00 EST) and bottomed at 41,040 MW ..."."""
    clauses = []
    for key, ((high_when, high), (low_when, low)) in ranged.items():
        label, unit = _MEASURES[key]
        unit = f" {unit}" if unit else ""
        clauses.append(f"{label} peaked at {high:,.0f}{unit} ({_clock(high_when)} EST) "
                       f"and bottomed at {low:,.0f}{unit} ({_clock(low_when)} EST)")
    return "; ".join(clauses)


def _rows_by_region(rows: list) -> dict:
    """Every row of the day, grouped by region code."""
    grouped: dict = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("region"):
            continue
        grouped.setdefault(str(row["region"]).upper(), []).append(row)
    return grouped


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


def _describe_rows(rows_here: list) -> str:
    """The measures on one region's rows at one interval, as a phrase."""
    described = []
    for row in rows_here:
        measures = _measures(row)
        dimension = _dimension_label(row)
        if dimension and measures:
            described.append(f"{dimension} ({measures})")
        elif dimension or measures:
            described.append(dimension or measures)
    return "; ".join(described)


# How a feed's day is described. A forecast is not a settlement, and calling
# one "a settled market day, not live output" told the reader the opposite of
# the truth about the only forward-looking feed in the set.
FRAMING = {
    "settled": ("the completed market day",
                "This is a settled market day, not live output."),
    "forecast": ("the forecast day",
                 "This is a forecast MISO published in advance, not measured output."),
}


def _interval_total_phrase(rows_here: list) -> str:
    """", 17,790 MW in total" when several rows share the region's interval."""
    if len(rows_here) < 2:
        return ""
    summed: dict = {}
    for row in rows_here:
        for key, amount in _row_measures(row).items():
            summed[key] = summed.get(key, 0.0) + amount
    parts = []
    for key, amount in summed.items():
        label, unit = _MEASURES[key]
        unit = f" {unit}" if unit else ""
        parts.append(f"{amount:,.0f}{unit} {label} in total")
    return ", " + ", ".join(parts) if parts else ""


def make_de_transformer(title: str, doc_url: str, kind: str = "settled"):
    """A transformer for one region-scoped Data Exchange endpoint.

    Same contract as every other transformer: (data) -> (prose, as_of, url).

    Each region gets the day's high and low plus its own final interval. Both
    halves are load-bearing. The range is there because a settled market day
    is a day, and answering "what was the load yesterday" with the 23:00 value
    understated MISO Central by 25%. The per-region interval is there because
    regions do not end together - on one real fuel-on-the-margin payload
    CENTRAL ended at 23:55 and NO_REGION at 14:20, and stamping the newest of
    those across all of them put a timestamp on numbers it did not belong to.
    """
    def transform(data: dict) -> tuple[str, str, str]:
        rows = [r for r in (data.get("data") if isinstance(data, dict) else None) or []
                if isinstance(r, dict)]
        latest = _rows_by_region_at_latest_interval(rows)
        if not latest:
            return (f"No MISO Data Exchange data is available for {title}.", "", doc_url)

        by_region = _rows_by_region(rows)
        # the document's freshness is its newest interval; each region still
        # carries its own below, so this stamps nothing it does not cover
        as_of = max((when for when, _ in latest.values()), default="")

        day, nature = FRAMING.get(kind, FRAMING["settled"])
        # Name the date. The old header carried one and this replaced it with
        # per-region clock times, which left "ending 23:00 EST" with no day
        # attached - and the retriever hands Claude the text only, never the
        # as_of metadata, so nothing downstream could supply it. On demo day a
        # reader would take a two-day-old market day for yesterday.
        dated = f"{day} {as_of[:10]}" if "T" in as_of else day
        lines = [f"MISO {title} by region for {dated}, from the MISO Data Exchange "
                 f"API. {nature} Each region reports the day's high and low and "
                 f"its own final interval; regions can end at different intervals."]
        known = ("MISO", "NORTH", "CENTRAL", "SOUTH", "NO_REGION")
        order = [r for r in known if r in latest] + sorted(set(latest) - set(known))
        for code in order:
            when, rows_here = latest[code]
            name = _REGION_NAMES.get(code, code)
            final = _describe_rows(rows_here)
            clock = _clock(when)
            ending = f" ending {clock} EST" if clock else ""
            # A dimensional feed splits the interval over several rows, and the
            # day range below sums them - so without this the same label named
            # a zone's value and a region's total in one sentence, and the
            # region's own final number appeared nowhere.
            interval_total = _interval_total_phrase(rows_here)
            line = (f"- {name}: {final}{ending}{interval_total}." if final
                    else f"- {name}: no values reported.")
            day = _day_sentence(_day_range(by_region.get(code, [])))
            if day:
                line += f" Across the day, {day}."
            lines.append(line)
        return "\n".join(lines), as_of, doc_url
    return transform
