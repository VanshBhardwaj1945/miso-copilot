"""GET /crosswalk.csv - the report-to-API crosswalk as a spreadsheet-ready download."""

import csv
import io
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter()

CROSSWALK_PATH = Path(__file__).resolve().parent.parent / "rag" / "crosswalk.json"

COLUMNS = ["report", "old_file", "old_column", "api", "endpoint", "base_url",
           "new_field", "note", "params", "shape", "readers_guide"]


def crosswalk_rows() -> list[dict]:
    """One row per field mapping - the shape a trader pastes into a spreadsheet."""
    entries = json.loads(CROSSWALK_PATH.read_text(encoding="utf-8"))["entries"]
    rows = []
    for e in entries:
        params = " ".join(f"{k}={v}" for k, v in e.get("params", {}).items())
        for f in e["fields"]:
            rows.append({
                "report": e["report"], "old_file": e.get("old_file", ""),
                "old_column": f["old"], "api": e["api"], "endpoint": e["endpoint"],
                "base_url": e["base_url"], "new_field": f["new"], "note": f.get("note", ""),
                "params": params, "shape": e.get("shape", ""), "readers_guide": e["report_url"],
            })
    return rows


@router.get("/crosswalk.csv")
def crosswalk_csv():
    """The whole crosswalk as CSV; opens straight in Excel."""
    if not CROSSWALK_PATH.exists():
        raise HTTPException(404, "crosswalk.json has not been built - see backend/rag/README.md")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(crosswalk_rows())
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="miso-report-to-api-crosswalk.csv"'},
    )
