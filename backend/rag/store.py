"""Chroma vector store and LlamaIndex setup (local embeddings, no API cost)."""

import threading
from pathlib import Path

import chromadb
from llama_index.core import Settings, StorageContext, VectorStoreIndex
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore

# anchored to repo root / data / chroma
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHROMA_DIR = REPO_ROOT / "data" / "chroma"
COLLECTION_NAME = "miso_copilot_store"

# configure all-MiniLM-L6-v2 as specified in repo architecture
Settings.embed_model = HuggingFaceEmbedding(
    model_name="sentence-transformers/all-MiniLM-L6-v2"
)
Settings.chunk_size = 512
Settings.chunk_overlap = 50


# One client for the process, built once, behind a lock.
#
# This used to construct a PersistentClient on every call - roughly thirty per
# poll cycle, on the poller thread, while /ask was reading on request threads.
# Chroma caches the shared system in an unlocked class dict and releases it for
# everyone when a construction fails, so the two raced: measured at about 1 in
# 100 reads, surfacing as KeyError on the cache, "Could not connect to tenant
# default_tenant", or a missing attribute on the Rust bindings. routes/ask.py
# catches only anthropic errors, so each one escaped as a 500 and the UI showed
# the "couldn't reach the data service" handoff. Construction costs ~19 ms, so
# reusing it is free.
_client = None
_client_lock = threading.Lock()


def reset_client() -> None:
    """Drop the cached client. For tests, which repoint CHROMA_DIR per test -
    a client built against the previous directory would quietly serve it."""
    global _client
    with _client_lock:
        _client = None


def get_chroma_collection():
    global _client
    with _client_lock:
        if _client is None:
            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        return _client.get_or_create_collection(COLLECTION_NAME)


def get_vector_store() -> ChromaVectorStore:
    collection = get_chroma_collection()
    return ChromaVectorStore(chroma_collection=collection)


def get_index() -> VectorStoreIndex:
    vector_store = get_vector_store()
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    return VectorStoreIndex.from_vector_store(
        vector_store=vector_store,
        storage_context=storage_context
    )