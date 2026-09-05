'''
Transform raw MISO JSON payloads into plain-English
'''

from typing import Any

MISO_DISPLAY_URL = (
    "https://www.misoenergy.org/markets-and-operations/"
    "real-time--market-data/real-time-displays/"
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
        category = item.get("FUEL_CATEGORY") or item.get("CATEGORY") or "Unknown"
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