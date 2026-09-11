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


# --- the Data Exchange half of the seam -----------------------------------

def de_payload(**values):
    """One region, two intervals: enough for a day range as well as an end state."""
    return {"data": [
        {"timeInterval": {"start": "2026-09-09T16:00:00", "value": "17"},
         "region": "CENTRAL", **{k: v[0] for k, v in values.items()}},
        {"timeInterval": {"start": "2026-09-09T23:00:00", "value": "24"},
         "region": "CENTRAL", **{k: v[1] for k, v in values.items()}},
    ]}


@pytest.fixture
def keyed(monkeypatch):
    """Data Exchange feeds are registered only when a key is configured."""
    monkeypatch.setenv("MISO_API_KEY", "test-subscription-key")


def test_a_settled_feed_becomes_a_settled_document(raw_dir, keyed):
    """The seam nothing exercised: every test here used FuelMix, so the
    settled arm of _doc_type_for was never once executed. Replacing its body
    with a raise left all 883 tests passing."""
    (raw_dir / "DEActualLoad.json").write_text(
        json.dumps(de_payload(load=(54527.0, 41040.0))))
    results = ingest_api.sync_raw_snapshots(raw_dir)
    assert results["DEActualLoad.json"] is True

    got = store.get_chroma_collection().get(
        where={"doc_id": "miso_snapshot_deactualload"},
        include=["documents", "metadatas"])
    meta = got["metadatas"][0]
    assert meta["doc_type"] == "settled_market_day"
    assert meta["title"] == "MISO Data Exchange - actual load (completed market day)"
    # this feed's own operation, not the fuel-type page every feed used to cite
    assert meta["source_url"].endswith("operation=get-v1-real-time-date-demand-actual")
    assert "peaked at 54,527 MW" in got["documents"][0]


def test_a_settled_document_is_never_titled_a_real_time_display(raw_dir, keyed):
    """The citation label is on screen. "Real-Time Display" over yesterday's
    settled load undoes everything the prose works to say."""
    (raw_dir / "DENetScheduled.json").write_text(json.dumps(de_payload(nsi=(10.0, 20.0))))
    ingest_api.sync_raw_snapshots(raw_dir)
    got = store.get_chroma_collection().get(
        where={"doc_id": "miso_snapshot_denetscheduled"}, include=["metadatas"])
    assert "Real-Time Display" not in got["metadatas"][0]["title"]


def test_the_forecast_feed_is_not_labeled_a_completed_market_day(raw_dir, keyed):
    """A forecast is not a settlement, and it was being cited as one."""
    (raw_dir / "DELoadForecast.json").write_text(
        json.dumps(de_payload(loadForecast=(500.0, 400.0))))
    ingest_api.sync_raw_snapshots(raw_dir)
    got = store.get_chroma_collection().get(
        where={"doc_id": "miso_snapshot_deloadforecast"},
        include=["documents", "metadatas"])
    assert got["metadatas"][0]["title"].endswith("(forecast)")
    assert "settled market day" not in got["documents"][0]


def test_every_settled_feed_cites_its_own_operation(keyed):
    """Rule 6 says the source URL is the product. One shared anchor also made
    the retriever's dedupe-by-URL collapse four feeds into one citation."""
    urls = {key: ingest_api.de_doc_url(key) for key in ingest_api.DE_FEEDS}
    assert len(set(urls.values())) == len(ingest_api.DE_FEEDS)


def test_without_a_key_the_data_exchange_feeds_are_not_expected(monkeypatch):
    """No key is a supported configuration. Asking for eleven files the poller
    never fetches logged eleven warnings and an "incomplete re-sync" every five
    minutes, which trains everyone to ignore a warning that meant something."""
    monkeypatch.delenv("MISO_API_KEY", raising=False)
    expected = ingest_api.expected_endpoints()
    assert set(expected) == {"FuelMix.json", "RealTimeTotalLoad.json",
                             "Snapshot.json", "WindSolar.json"}


def test_with_a_key_every_registered_feed_is_expected(keyed):
    assert set(ingest_api.expected_endpoints()) == set(ingest_api.ENDPOINTS_CONFIG)
