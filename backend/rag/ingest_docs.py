'''
static document chunking and ingestion (one-time)
'''

from pathlib import Path
from llama_index.core import SimpleDirectoryReader
from llama_index.core.node_parser import SentenceSplitter
from backend.rag.store import get_chroma_collection, get_index

DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "docs"

def ingest_general_docs():
    """One-time ingestion of MISO reference documents into Chroma."""
    if not DOCS_DIR.exists() or not any(DOCS_DIR.iterdir()):
        print(f"No documents found in {DOCS_DIR}")
        return

    # load all markdown and text files from data/docs
    reader = SimpleDirectoryReader(input_dir=str(DOCS_DIR))
    documents = reader.load_data()

    # chunk them
    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    
    # evict prior reference docs so startup/reload is idempotent
    collection = get_chroma_collection()
    try:
        collection.delete(where={"doc_type": "reference_doc"})
    except Exception:
        pass

    # add to the SAME Chroma collection
    index = get_index()
    for doc in documents:
        # add metadata for citations
        doc.metadata["doc_type"] = "reference_doc"
        doc.metadata["source_url"] = doc.metadata.get("file_name", "MISO Reference Document")
        
        nodes = splitter.get_nodes_from_documents([doc])
        index.insert_nodes(nodes)
        
    print(f"Successfully ingested {len(documents)} reference documents into Chroma!")

if __name__ == "__main__":
    ingest_general_docs()