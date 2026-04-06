"""Milvus 向量服务：知识切片、向量化、语义检索。"""
from __future__ import annotations

import logging
import threading

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from app.config import settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "knowledge_chunks"
DIM = 1024  # BAAI/bge-m3 dense vector dimension

_collection: Collection | None = None
_embed_model = None
_init_lock = threading.Lock()  # 防止并发初始化


def _sanitize_milvus_str(value: str) -> str:
    """转义 Milvus filter expression 中的字符串值，防止表达式注入。
    Milvus 字符串值用双引号包裹，需要转义内部的双引号和反斜杠。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ─── Embedding ────────────────────────────────────────────────────────────────

_BGE_M3_PATH = "/home/mo/.cache/modelscope/BAAI/bge-m3"


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from FlagEmbedding import BGEM3FlagModel  # lazy import (heavy)
        _embed_model = BGEM3FlagModel(_BGE_M3_PATH, use_fp16=True)
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


def _is_connection_healthy() -> bool:
    """检查 Milvus 连接是否健康。"""
    try:
        # list_collections 是轻量操作，用来验证连接
        utility.list_collections()
        return True
    except Exception:
        return False


def _reconnect():
    """断开后重新连接 Milvus。"""
    global _collection
    try:
        connections.disconnect("default")
    except Exception:
        pass
    _collection = None
    _connect()


def get_collection() -> Collection:
    global _collection

    # 快速路径：已缓存 + 连接健康
    if _collection is not None:
        if _is_connection_healthy():
            return _collection
        # 连接不健康，重连
        logger.warning("Milvus connection unhealthy, reconnecting...")
        _reconnect()

    # 加锁防止并发初始化
    with _init_lock:
        # double-check
        if _collection is not None:
            return _collection
        _connect()

    if not utility.has_collection(COLLECTION_NAME):
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="knowledge_id", dtype=DataType.INT64),
            FieldSchema(name="chunk_index", dtype=DataType.INT64),
            FieldSchema(name="created_by", dtype=DataType.INT64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8000),
            FieldSchema(name="desensitized_text", dtype=DataType.VARCHAR, max_length=8000),
            # 分类 metadata（用于过滤检索）
            FieldSchema(name="taxonomy_board", dtype=DataType.VARCHAR, max_length=10, default_value=""),
            FieldSchema(name="taxonomy_code", dtype=DataType.VARCHAR, max_length=20, default_value=""),
            FieldSchema(name="file_type", dtype=DataType.VARCHAR, max_length=50, default_value=""),
            FieldSchema(name="quality_score", dtype=DataType.FLOAT, default_value=0.5),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=DIM),
        ]
        schema = CollectionSchema(fields, description="Knowledge chunks with metadata for filtered RAG retrieval")
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


# ─── Desensitisation ─────────────────────────────────────────────────────────

def _desensitize_chunks_llm(chunks: list[str], db=None) -> list[str]:
    """使用 text_masker 以 D2 级别预脱敏，fallback 到规则兜底。"""
    try:
        from app.services.text_masker import mask_text
        return [mask_text(c, level="D2")[0] for c in chunks]
    except Exception as e:
        logger.warning(f"text_masker desensitize failed, fallback to rules: {e}")
        return [_desensitize_rule(c) for c in chunks]


def _desensitize_rule(text: str) -> str:
    """Rule-based fallback: regex replacement for common sensitive patterns."""
    import re
    # 金额
    text = re.sub(r"[\d,]+\.?\d*\s*[万亿元美元]", "若干金额", text)
    # 百分比
    text = re.sub(r"\d+\.?\d*\s*%", "一定比例", text)
    # 电话
    text = re.sub(r"1[3-9]\d{9}", "***电话***", text)
    # 邮箱
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "***邮箱***", text)
    # 连续数字 (>= 4 位，可能是 ID、账号等)
    text = re.sub(r"\b\d{4,}\b", "****", text)
    return text


# ─── Public API ───────────────────────────────────────────────────────────────

def index_knowledge(
    knowledge_id: int,
    text: str,
    created_by: int = 0,
    taxonomy_board: str = "",
    taxonomy_code: str = "",
    file_type: str = "",
    quality_score: float = 0.5,
    db=None,
) -> list[int]:
    """Chunk text, embed, desensitize, and insert into Milvus with metadata.

    如果传入 db 且能获取到 KnowledgeEntry，优先走 block→chunk 流水线，
    自动生成 document_blocks 和 chunk_mappings。否则回退到原始 chunk_text。
    """
    col = get_collection()

    # 尝试走 block→chunk 流水线
    block_chunks = None
    if db is not None:
        try:
            from app.models.knowledge import KnowledgeEntry
            from app.services.block_splitter import generate_blocks_and_chunks
            entry = db.get(KnowledgeEntry, knowledge_id)
            if entry:
                block_chunks = generate_blocks_and_chunks(db, entry)
        except Exception as e:
            logger.warning(f"Block-based chunking failed for {knowledge_id}, falling back: {e}")

    if block_chunks:
        chunks = [c["text"] for c in block_chunks]
    else:
        chunks = chunk_text(text)

    if not chunks:
        return []

    n = len(chunks)
    embeddings = embed_texts(chunks)

    # Generate desensitized versions
    desensitized = _desensitize_chunks_llm(chunks, db=db)

    result = col.insert([
        [knowledge_id] * n,              # knowledge_id
        list(range(n)),                   # chunk_index
        [created_by] * n,                # created_by
        chunks,                           # text
        desensitized,                     # desensitized_text
        [taxonomy_board or ""] * n,       # taxonomy_board
        [taxonomy_code or ""] * n,        # taxonomy_code
        [file_type or ""] * n,            # file_type
        [quality_score] * n,              # quality_score
        embeddings,                       # embedding
    ])
    col.flush()

    # 回写 milvus_chunk_id 到 chunk_mappings
    if block_chunks and db is not None:
        try:
            from app.models.knowledge_block import KnowledgeChunkMapping
            milvus_ids = list(result.primary_keys)
            mappings = (
                db.query(KnowledgeChunkMapping)
                .filter(KnowledgeChunkMapping.knowledge_id == knowledge_id)
                .order_by(KnowledgeChunkMapping.chunk_index)
                .all()
            )
            for mapping, mid in zip(mappings, milvus_ids):
                mapping.milvus_chunk_id = str(mid)
            db.flush()
        except Exception as e:
            logger.warning(f"Failed to update milvus_chunk_id for {knowledge_id}: {e}")

    return list(result.primary_keys)


def search_knowledge(
    query: str,
    top_k: int = 8,
    knowledge_id_filter: list[int] = None,
    taxonomy_board: str = None,
    file_type: str = None,
    min_quality: float = None,
) -> list[dict]:
    """Semantic search with optional metadata filtering.

    Returns list of {knowledge_id, chunk_index, text, desensitized_text,
    created_by, taxonomy_board, taxonomy_code, quality_score, score}.
    """
    col = get_collection()
    q_embedding = embed_query(query)

    # 构建过滤表达式（参数化防注入）
    exprs = []
    if knowledge_id_filter:
        # 确保全部为整数
        safe_ids = [int(i) for i in knowledge_id_filter]
        ids_str = ", ".join(str(i) for i in safe_ids)
        exprs.append(f"knowledge_id in [{ids_str}]")
    if taxonomy_board:
        exprs.append(f'taxonomy_board == "{_sanitize_milvus_str(taxonomy_board)}"')
    if file_type:
        exprs.append(f'file_type == "{_sanitize_milvus_str(file_type)}"')
    if min_quality is not None:
        exprs.append(f"quality_score >= {float(min_quality)}")

    expr = " and ".join(exprs) if exprs else None

    output_fields = [
        "knowledge_id", "chunk_index", "text", "desensitized_text",
        "created_by", "taxonomy_board", "taxonomy_code", "quality_score",
    ]

    results = col.search(
        data=[q_embedding],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"ef": 128}},
        limit=top_k,
        expr=expr,
        output_fields=output_fields,
    )

    hits = []
    for hit in results[0]:
        quality = float(hit.entity.get("quality_score", 0.5))
        cosine_score = float(hit.score)
        # 质量加权最终分数：cosine_sim * 0.8 + quality_score * 0.2
        final_score = cosine_score * 0.8 + quality * 0.2

        hits.append({
            "knowledge_id": hit.entity.get("knowledge_id"),
            "chunk_index": hit.entity.get("chunk_index"),
            "text": hit.entity.get("text"),
            "desensitized_text": hit.entity.get("desensitized_text", ""),
            "created_by": hit.entity.get("created_by", 0),
            "taxonomy_board": hit.entity.get("taxonomy_board", ""),
            "taxonomy_code": hit.entity.get("taxonomy_code", ""),
            "quality_score": quality,
            "score": round(final_score, 4),
            "cosine_score": round(cosine_score, 4),
        })

    # 按加权分数重排序
    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits


def delete_knowledge_vectors(knowledge_id: int) -> None:
    """Delete all vectors for a given knowledge entry."""
    col = get_collection()
    safe_id = int(knowledge_id)  # 确保为整数，防止注入
    col.delete(f"knowledge_id == {safe_id}")
    col.flush()
