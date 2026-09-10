"""sync_raw_snapshots: raw poller JSON -> one prose document per endpoint.

This is the seam between the poller and the vector store, and it had no tests.
Everything runs against a temporary Chroma directory, so no test touches the
real store.
"""

import json

import pytest

from backend.rag import ingest_api, store


@pytest.fixture(autouse=True)
def isolated_chroma(tmp_path, monkeypatch):
    """A Chroma per test. Without this a test run would rewrite the live store."""
    monkeypatch.setattr(store, "CHROMA_DIR", tmp_path / "chroma")
    # BACKUP_RAW_DIR points at the repo's real data/raw.backup, so a test for
    # "this payload is missing" would quietly find a production file instead.
    monkeypatch.setattr(ingest_api, "BACKUP_RAW_DIR", tmp_path / "no-backup")


@pytest.fixture
def raw_dir(tmp_path):
    """conftest's poller_env already made tmp_path/raw, so do not remake it."""
    d = tmp_path / "raw"
    d.mkdir(exist_ok=True)
    return d


def fuelmix(ref="10-Sep-2026 - Interval 09:25 EST", total="82,059"):
    return {"RefId": ref, "TotalMW": total,
            "Fuel": {"Type": [{"CATEGORY": "Coal", "ACT": "29,039"},
                              {"CATEGORY": "Wind", "ACT": "1,500"}]}}


def test_a_snapshot_becomes_one_document_under_its_fixed_id(raw_dir):
    (raw_dir / "FuelMix.json").write_text(json.dumps(fuelmix()))
    results = ingest_api.sync_raw_snapshots(raw_dir)
    assert results["FuelMix.json"] is True

    got = store.get_chroma_collection().get(where={"doc_id": "miso_snapshot_fuelmix"},
                                            include=["documents", "metadatas"])
    assert len(got["ids"]) == 1
    assert "Coal" in got["documents"][0]
    assert got["metadatas"][0]["doc_type"] == "live_snapshot"
    assert got["metadatas"][0]["as_of"]


def test_re_syncing_overwrites_rather_than_appends(raw_dir):
    """Rule 3. Appending would leave the last cycle's numbers in the store,
    scoring as well as the current ones and reading just as current."""
    (raw_dir / "FuelMix.json").write_text(json.dumps(fuelmix(total="82,059")))
    ingest_api.sync_raw_snapshots(raw_dir)
    (raw_dir / "FuelMix.json").write_text(json.dumps(fuelmix(ref="later", total="99,999")))
    ingest_api.sync_raw_snapshots(raw_dir)

    got = store.get_chroma_collection().get(where={"doc_id": "miso_snapshot_fuelmix"},
                                            include=["documents"])
    assert len(got["ids"]) == 1
    assert "99,999" in got["documents"][0]
    assert "82,059" not in got["documents"][0]


def test_a_missing_payload_is_skipped_not_fatal(raw_dir):
    """One dead feed must not stop the others reaching the store."""
    (raw_dir / "FuelMix.json").write_text(json.dumps(fuelmix()))
    results = ingest_api.sync_raw_snapshots(raw_dir)
    assert results["FuelMix.json"] is True
    assert results["Snapshot.json"] is False


def test_malformed_json_does_not_take_the_sync_down(raw_dir):
    (raw_dir / "FuelMix.json").write_text("{ not json")
    (raw_dir / "WindSolar.json").write_text(json.dumps(
        {"RefId": "x", "MktDay": "09-10-2026", "instance": []}))
    results = ingest_api.sync_raw_snapshots(raw_dir)
    assert results["FuelMix.json"] is False
    assert results["WindSolar.json"] is True


def test_an_empty_file_counts_as_missing(raw_dir):
    (raw_dir / "FuelMix.json").write_text("")
    assert ingest_api.sync_raw_snapshots(raw_dir)["FuelMix.json"] is False


def test_every_configured_endpoint_names_a_transformer_and_a_doc_id():
    for filename, (doc_id, fn) in ingest_api.ENDPOINTS_CONFIG.items():
        assert filename.endswith(".json")
        assert doc_id.startswith("miso_snapshot_")
        assert callable(fn)


def test_doc_ids_are_unique():
    """Two endpoints sharing an id would silently overwrite each other."""
    ids = [doc_id for doc_id, _ in ingest_api.ENDPOINTS_CONFIG.values()]
    assert len(ids) == len(set(ids))
