"""Read raw poller JSON and UPSERT into Chroma: one doc per endpoint, fixed id."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from llama_index.core.schema import (
    NodeRelationship,
    RelatedNodeInfo,
    TextNode,
)

# imported for its side effect: backend.config loads .env into os.environ.
# Without it a caller that has not already imported config reads no key and
# silently drops all eleven Data Exchange feeds. core.py carries the same
# import for the same reason.
import backend.config  # noqa: F401

from backend.rag.store import get_chroma_collection, get_index
from backend.rag.transformers import (
    make_de_transformer,
    transform_de_fueltype,
    transform_fuelmix,
    transform_load,
    transform_snapshot,
    transform_windsolar,
)

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The default only. Callers that know where the poller actually wrote - the
# scheduler and app boot both do, via core.raw_dir() - pass it in, because
# MISO_RAW_DIR moves the poller's output and nothing here can see that env
# var without duplicating the rule that resolves it.
PRIMARY_RAW_DIR = REPO_ROOT / "data" / "raw"
BACKUP_RAW_DIR = REPO_ROOT / "data" / "raw.backup"

# The Data Exchange feeds: title for the citation, and the portal operation
# that produced the data. Each needs its own operation anchor - pointing every
# feed at the fuel-type operation sends someone asking about the load forecast
# to a page that does not mention it, and rule 6 says the source URL is the
# product.
DE_FEEDS: dict[str, tuple[str, str, str]] = {
    "DEFuelMix": ("real-time generation by fuel type",
                  "get-v1-real-time-date-generation-fuel-type", "settled"),
    "DEActualLoad": ("actual load", "get-v1-real-time-date-demand-actual", "settled"),
    "DEFuelOnMargin": ("fuel on the margin",
                       "get-v1-real-time-date-generation-fuel-on-the-margin", "settled"),
    "DEDayAheadDemand": ("day-ahead cleared demand", "get-v1-day-ahead-date-demand", "settled"),
    "DEDayAheadFuelMix": ("day-ahead generation by fuel type",
                          "get-v1-day-ahead-date-generation-fuel-type", "settled"),
    "DEClearedPhysical": ("day-ahead cleared physical generation",
                          "get-v1-day-ahead-date-generation-cleared-physical", "settled"),
    "DEClearedVirtual": ("day-ahead cleared virtual generation",
                         "get-v1-day-ahead-date-generation-cleared-virtual", "settled"),
    "DEOfferedEcoMax": ("day-ahead offered generation, economic maximum",
                        "get-v1-day-ahead-date-generation-offered-ecomax", "settled"),
    "DEOfferedEcoMin": ("day-ahead offered generation, economic minimum",
                        "get-v1-day-ahead-date-generation-offered-ecomin", "settled"),
    "DENetScheduled": ("day-ahead net scheduled interchange",
                       "get-v1-day-ahead-date-interchange-net-scheduled", "settled"),
    "DELoadForecast": ("medium-term load forecast", "get-v1-forecast-date-load", "forecast"),
}
DE_TITLES = {key: title for key, (title, _, _kind) in DE_FEEDS.items()}
DE_KINDS = {key: kind for key, (_t, _a, kind) in DE_FEEDS.items()}


def de_doc_url(key: str) -> str:
    """The portal page for the operation that produced this feed."""
    operation = DE_FEEDS[key][1]
    return ("https://data-exchange.misoenergy.org/api-details"
            "#api=load-generation-and-interchange-api"
            f"&operation={operation}")


# Endpoint JSON file -> (fixed Chroma doc id, JSON->prose transformer).
ENDPOINTS_CONFIG: dict[str, tuple[str, Callable[[Any], tuple[str, str, str]]]] = {
    "FuelMix.json": ("miso_snapshot_fuelmix", transform_fuelmix),
    "RealTimeTotalLoad.json": ("miso_snapshot_load", transform_load),
    "Snapshot.json": ("miso_snapshot_snapshot", transform_snapshot),
    "WindSolar.json": ("miso_snapshot_windsolar", transform_windsolar),
    # Data Exchange, present only once a subscription key is configured. Same
    # treatment as the four above: one fixed doc id, overwritten each cycle.
    # fuel-type keeps its own transformer because a fuelTypes breakdown reads
    # better than a flat list; the rest share the generic one.
    "DEFuelMix.json": ("miso_snapshot_de_fueltype", transform_de_fueltype),
    **{
        f"{key}.json": (f"miso_snapshot_{key.lower()}",
                        make_de_transformer(title, de_doc_url(key), kind))
        for key, (title, _anchor, kind) in DE_FEEDS.items() if key != "DEFuelMix"
    },
}


def expected_endpoints() -> dict:
    """The endpoints this machine should actually have data for.

    Without a subscription key the poller never fetches Data Exchange, so
    asking for those files logs eleven "not found" warnings and reports the
    re-sync incomplete on every five-minute cycle - which trains everyone to
    ignore a warning that used to mean something. No key is a supported
    configuration, not a degraded one: the four display feeds are the demo's
    proven path.
    """
    if os.environ.get("MISO_API_KEY"):
        return dict(ENDPOINTS_CONFIG)
    return {name: value for name, value in ENDPOINTS_CONFIG.items()
            if not name.startswith(SETTLED_PREFIX)}


def _resolve_raw_file(filename: str, raw_dir: Path | None = None) -> Path | None:
    """The primary raw file, or the demo backup if it is missing/empty."""
    primary = (raw_dir or PRIMARY_RAW_DIR) / filename
    if primary.exists() and primary.stat().st_size > 0:
        return primary
    backup = BACKUP_RAW_DIR / filename
    if backup.exists() and backup.stat().st_size > 0:
        log.warning("Primary %s missing; falling back to demo backup %s",
                    filename, backup)
        return backup
    return None


# Data Exchange serves a completed market day; the four display feeds serve
# now. They answer different questions and must not compete for the same
# retrieval seats - measured, they do: the eleven settled documents share an
# opening sentence, embed as a block (0.75 mean cosine against 0.60 for the
# live feeds), and swept every seat on "total generation right now".
SETTLED_PREFIX = "DE"


def _doc_type_for(filename: str) -> str:
    return "settled_market_day" if filename.startswith(SETTLED_PREFIX) else "live_snapshot"


def upsert_single_endpoint(filename: str, doc_id: str,
                           transformer: Callable,
                           raw_dir: Path | None = None) -> bool:
    """Read one endpoint file, render it as prose, and UPSERT it into Chroma."""
    path = _resolve_raw_file(filename, raw_dir)
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

    # "Real-Time Display" on a settled market day undoes what the prose works
    # to say. The citation label is shown to the user; it has to agree.
    key = filename.replace(".json", "")
    # "(completed market day)" on the load forecast said the opposite of the
    # truth: a forecast is not a settlement.
    qualifier = {"forecast": "forecast"}.get(DE_KINDS.get(key), "completed market day")
    title = (f"MISO Data Exchange - {DE_TITLES.get(key, key)} ({qualifier})"
             if _doc_type_for(filename) == "settled_market_day"
             else f"MISO {key} Real-Time Display")

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

    # NO CHUNKING - architecture rule 7, and insert_nodes is what enforces it.
    # index.insert() runs the Settings splitter, which is sized for the 512-token
    # reference chunks, and the day ranges pushed three of these documents past
    # it: DEFuelMix, DEOfferedEcoMax and DEOfferedEcoMin each split in two.
    # Nothing surfaced, because the retriever keeps one document per endpoint -
    # so the second chunk was silently unreachable, and being the tail of the
    # prose it was always the last region's numbers. MISO South lost its day
    # range and Claude said so: "no intraday high/low was returned".
    node = TextNode(
        id_=doc_id,
        text=prose,
        metadata={
            "doc_type": _doc_type_for(filename),
            "endpoint": filename.replace(".json", ""),
            "as_of": as_of,
            "source_url": source_url,
            "title": title,
        },
        excluded_embed_metadata_keys=["source_url", "title", "doc_type"],
    )
    # The Chroma row's doc_id comes from the node's source relationship, not
    # from its metadata - index.insert() used to set this for us. Without it
    # every row lands with doc_id null, the UPSERT below finds nothing to
    # evict, and the store grows a duplicate every cycle.
    node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=doc_id)

    index.insert_nodes([node])

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


def sync_raw_snapshots(raw_dir: Path | None = None) -> dict[str, bool]:
    """Sync every raw API snapshot from disk into Chroma. Returns per-file success.

    raw_dir defaults to data/raw. Pass core.raw_dir() to follow MISO_RAW_DIR,
    or Chroma is fed the default directory while the poller writes elsewhere.
    """
    results = {}
    for filename, (doc_id, transformer) in expected_endpoints().items():
        results[filename] = upsert_single_endpoint(filename, doc_id,
                                                   transformer, raw_dir)
    return results
