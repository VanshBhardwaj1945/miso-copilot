"""Read raw poller JSON and UPSERT into Chroma: one doc per endpoint, fixed id."""

import json
import logging
from pathlib import Path
from typing import Any, Callable

from llama_index.core.schema import Document

from backend.rag.store import get_chroma_collection, get_index
from backend.rag.transformers import (
    transform_fuelmix,
    transform_load,
    transform_snapshot,
    transform_windsolar,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PRIMARY_RAW_DIR = REPO_ROOT / "data" / "raw"
BACKUP_RAW_DIR = REPO_ROOT / "data" / "raw.backup"

# Endpoint JSON file -> (fixed Chroma doc id, JSON->prose transformer).
ENDPOINTS_CONFIG: dict[str, tuple[str, Callable[[Any], tuple[str, str, str]]]] = {
    "FuelMix.json": ("miso_snapshot_fuelmix", transform_fuelmix),
    "RealTimeTotalLoad.json": ("miso_snapshot_load", transform_load),
    "Snapshot.json": ("miso_snapshot_snapshot", transform_snapshot),
    "WindSolar.json": ("miso_snapshot_windsolar", transform_windsolar),
}


def _resolve_raw_file(filename: str) -> Path | None:
    """The primary raw file, or the demo backup if it is missing/empty."""
    primary = PRIMARY_RAW_DIR / filename
    if primary.exists() and primary.stat().st_size > 0:
        return primary
    backup = BACKUP_RAW_DIR / filename
    if backup.exists() and backup.stat().st_size > 0:
        log.warning("Primary %s missing; falling back to demo backup %s",
                    filename, backup)
        return backup
    return None


def upsert_single_endpoint(filename: str, doc_id: str,
                           transformer: Callable) -> bool:
    """Read one endpoint file, render it as prose, and UPSERT it into Chroma."""
    path = _resolve_raw_file(filename)
    if not path:
        log.warning("Endpoint file %s not found in raw or backup directories.",
                    filename)
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        prose, as_of, source_url = transformer(payload)
    except Exception as err:
        log.error("Failed to parse %s: %s", path, err)
        return False

    collection = get_chroma_collection()
    index = get_index()

    # The rows this write replaces, captured BEFORE inserting so the delete
    # below can name them exactly. LlamaIndex mints a fresh uuid per node, so
    # there is no stable row id to upsert against - this is how we get one.
    try:
        stale_ids = collection.get(where={"doc_id": doc_id}).get("ids", [])
    except Exception as err:
        log.debug("Could not list previous entries for %s (%s)", doc_id, err)
        stale_ids = []

    # no chunking - snapshots are already one small paragraph
    doc = Document(
        id_=doc_id,
        text=prose,
        metadata={
            "doc_type": "live_snapshot",
            "endpoint": filename.replace(".json", ""),
            "as_of": as_of,
            "source_url": source_url,
            "title": f"MISO {filename.replace('.json', '')} Real-Time Display",
        },
        excluded_embed_metadata_keys=["source_url", "title", "doc_type"],
    )

    index.insert(doc)

    # NEVER APPEND - evict the rows just replaced. Insert first, delete second:
    # deleting first leaves a window where a question retrieves no snapshot at
    # all, and the poller now writes every 5 minutes rather than once at boot.
    # This way the window holds a duplicate instead, which the retriever
    # already collapses by endpoint. A failed insert above deletes nothing, so
    # the old snapshot survives rather than the endpoint going dark.
    if stale_ids:
        try:
            collection.delete(ids=stale_ids)
        except Exception as err:
            log.warning("Could not evict previous entries for %s (%s)",
                        doc_id, err)
    log.info("Upserted snapshot '%s' (as of %s)", doc_id, as_of)
    return True


def sync_raw_snapshots() -> dict[str, bool]:
    """Sync every raw API snapshot from disk into Chroma. Returns per-file success."""
    results = {}
    for filename, (doc_id, transformer) in ENDPOINTS_CONFIG.items():
        results[filename] = upsert_single_endpoint(filename, doc_id, transformer)
    return results
