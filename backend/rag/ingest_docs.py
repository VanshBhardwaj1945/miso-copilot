"""Chunk the reference documents in data/docs/ into Chroma, with citations.

Run:  python -m backend.rag.fetch_docs     (once - downloads the corpus)
      python -m backend.rag.ingest_docs    (stop the backend first)

Every chunk carries doc_type "reference_doc", a title, and the real
misoenergy.org URL from doc_sources.json - the retriever only cites chunks
that have one, and the UI renders it as a clickable link. Re-running is safe:
all prior reference_doc chunks are evicted before the new ones go in.
"""

import json
import logging
from pathlib import Path

from llama_index.core import SimpleDirectoryReader
from llama_index.core.node_parser import SentenceSplitter

from backend.rag.store import get_chroma_collection, get_index

# pypdf warns per font that fontTools could parse encodings more fully; the
# text comes out fine without it, and 20 identical warnings hide real errors.
logging.getLogger("pypdf").setLevel(logging.ERROR)

HERE = Path(__file__).resolve().parent
SOURCES_PATH = HERE / "doc_sources.json"
DOCS_DIR = HERE.parent.parent / "data" / "docs"

# LlamaIndex stamps every file with these; none of them mean anything to a
# question, and a stray "/Users/vb/..." in the embedding only adds noise.
FILE_NOISE = ["file_path", "file_name", "file_type", "file_size",
              "creation_date", "last_modified_date", "page_label"]


def load_citations() -> dict[str, dict]:
    """filename -> {title, url} from the curated source list."""
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    return {src["file"]: src for src in sources}


def ingest_general_docs() -> int:
    """Load, chunk, and store every document in data/docs/. Returns chunk count."""
    have_files = DOCS_DIR.exists() and any(DOCS_DIR.glob("*.[pm]d*"))
    if not have_files:
        print(f"No documents in {DOCS_DIR} - run python -m backend.rag.fetch_docs first")
        return 0

    citations = load_citations()
    reader = SimpleDirectoryReader(input_dir=str(DOCS_DIR),
                                   required_exts=[".pdf", ".md"])
    documents = reader.load_data()

    # documents that help people FIND things get chunked; snapshots never are
    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)

    # evict the previous corpus first so re-running never duplicates
    collection = get_chroma_collection()
    try:
        collection.delete(where={"doc_type": "reference_doc"})
    except Exception:
        pass

    index = get_index()
    total = 0
    for doc in documents:
        filename = doc.metadata.get("file_name", "")
        cite = citations.get(filename)
        if cite is None:
            print(f"  skipping {filename}: not listed in doc_sources.json, no URL to cite")
            continue

        doc.metadata["doc_type"] = "reference_doc"
        doc.metadata["title"] = cite["title"]
        doc.metadata["source_url"] = cite["url"]
        doc.excluded_embed_metadata_keys = FILE_NOISE + ["source_url", "doc_type"]
        doc.excluded_llm_metadata_keys = FILE_NOISE + ["source_url", "doc_type"]

        nodes = splitter.get_nodes_from_documents([doc])
        index.insert_nodes(nodes)
        total += len(nodes)

    print(f"Ingested {total} chunks from {len(documents)} document pages "
          f"({len(citations)} sources) into Chroma")
    return total


if __name__ == "__main__":
    ingest_general_docs()
