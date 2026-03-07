"""Milvus 向量服务：知识切片、向量化、语义检索。"""
from __future__ import annotations

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.config import settings

COLLECTION_NAME = "knowledge_chunks"
DIM = 1024  # BAAI/bge-m3 dense vector dimension

_collection: Collection | None = None
_embed_model = None


# ─── Embedding ────────────────────────────────────────────────────────────────

def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from FlagEmbedding import BGEM3FlagModel  # lazy import (heavy)
        _embed_model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
    return _embed_model


def embed_texts(texts: list[str]) -> list[list[float]]:
    model = _get_embed_model()
    result = model.encode(texts, batch_size=16, max_length=512)
    return result["dense_vecs"].tolist()


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]


# ─── Milvus collection ────────────────────────────────────────────────────────

def _connect():
    connections.connect("default", host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)


def get_collection() -> Collection:
    global _collection
    if _collection is not None:
        return _collection

    _connect()

    if not utility.has_collection(COLLECTION_NAME):
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="knowledge_id", dtype=DataType.INT64),
            FieldSchema(name="chunk_index", dtype=DataType.INT64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8000),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=DIM),
        ]
        schema = CollectionSchema(fields, description="Knowledge chunks for semantic search")
        col = Collection(COLLECTION_NAME, schema)
        col.create_index(
            "embedding",
            {
                "index_type": "HNSW",
                "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 256},
            },
        )
        col.load()
        _collection = col
    else:
        col = Collection(COLLECTION_NAME)
        col.load()
        _collection = col

    return _collection


# ─── Chunking ─────────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 100) -> list[str]:
    """Simple character-based chunking with overlap."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += chunk_size - overlap
    return chunks


# ─── Public API ───────────────────────────────────────────────────────────────

def index_knowledge(knowledge_id: int, text: str) -> list[int]:
    """Chunk text, embed, and insert into Milvus. Returns list of primary key IDs."""
    col = get_collection()
    chunks = chunk_text(text)
    if not chunks:
        return []

    embeddings = embed_texts(chunks)

    result = col.insert([
        [knowledge_id] * len(chunks),   # knowledge_id
        list(range(len(chunks))),        # chunk_index
        chunks,                          # text
        embeddings,                      # embedding
    ])
    col.flush()
    return list(result.primary_keys)


def search_knowledge(
    query: str,
    top_k: int = 8,
    knowledge_id_filter: list[int] = None,
) -> list[dict]:
    """Semantic search. Returns list of {knowledge_id, chunk_index, text, score}."""
    col = get_collection()
    q_embedding = embed_query(query)

    expr = None
    if knowledge_id_filter:
        ids_str = ", ".join(str(i) for i in knowledge_id_filter)
        expr = f"knowledge_id in [{ids_str}]"

    results = col.search(
        data=[q_embedding],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"ef": 128}},
        limit=top_k,
        expr=expr,
        output_fields=["knowledge_id", "chunk_index", "text"],
    )

    hits = []
    for hit in results[0]:
        hits.append({
            "knowledge_id": hit.entity.get("knowledge_id"),
            "chunk_index": hit.entity.get("chunk_index"),
            "text": hit.entity.get("text"),
            "score": round(float(hit.score), 4),
        })
    return hits


def delete_knowledge_vectors(knowledge_id: int) -> None:
    """Delete all vectors for a given knowledge entry."""
    col = get_collection()
    col.delete(f"knowledge_id == {knowledge_id}")
    col.flush()
