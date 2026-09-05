'''
Chroma vector store and LlamaIndex initialization.
'''

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


def get_chroma_collection():
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(COLLECTION_NAME)


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