'''
Read raw poller JSON
 and UPSERT into Chroma via LlamaIndex
'''

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

# registry mapping endpoint JSON file to fixed doc_id and transformer
ENDPOINTS_CONFIG: dict[str, tuple[str, Callable[[Any], tuple[str, str, str]]]] = {
    "FuelMix.json": ("miso_snapshot_fuelmix", transform_fuelmix),
    "RealTimeTotalLoad.json": ("miso_snapshot_load", transform_load),
    "Snapshot.json": ("miso_snapshot_snapshot", transform_snapshot),
    "WindSolar.json": ("miso_snapshot_windsolar", transform_windsolar),
}


def _resolve_raw_file(filename: str) -> Path | None:
    """Prefer data/raw/, fallback to data/raw.backup/ if needed"""
    primary = PRIMARY_RAW_DIR / filename
    if primary.exists() and primary.stat().st_size > 0:
        return primary
    backup = BACKUP_RAW_DIR / filename
    if backup.exists() and backup.stat().st_size > 0:
        log.warning("Primary %s missing; falling back to demo backup %s", filename, backup)
        return backup
    return None


def upsert_single_endpoint(filename: str, doc_id: str, transformer: Callable) -> bool:
    """Reads one endpoint file, formats prose, and UPSERTs into Chroma."""
    path = _resolve_raw_file(filename)
    if not path:
        log.warning("Endpoint file %s not found in raw or backup directories.", filename)
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        prose, as_of, source_url = transformer(payload)
    except Exception as err:
        log.error("Failed to parse %s: %s", path, err)
        return False

    collection = get_chroma_collection()
    index = get_index()

    # NEVER APPEND. Evict old document with this doc_id
    try:
        collection.delete(where={"doc_id": doc_id})
        log.info("Evicted prior snapshot for %s", doc_id)
    except Exception as err:
        log.debug("No previous entry to delete for %s (%s)", doc_id, err)

    # build unchunked Document with fixed doc_id
    doc = Document(
        doc_id=doc_id,
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
    log.info("Successfully upserted snapshot '%s' (as of %s)", doc_id, as_of)
    return True


def sync_raw_snapshots() -> dict[str, bool]:
    """Sync all available API JSON snapshots from disk into Chroma."""
    results = {}
    for filename, (doc_id, transformer) in ENDPOINTS_CONFIG.items():
        results[filename] = upsert_single_endpoint(filename, doc_id, transformer)
    return results