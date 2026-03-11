"""
迁移脚本：重建 Milvus knowledge_chunks collection
- 新增 created_by 字段
- 新增 desensitized_text 字段
- 从 MySQL knowledge_entries 回刷所有已审批的知识

用法：
    cd /Users/xia/project/universal-kb/backend
    /Users/xia/miniconda3/bin/python scripts/migrate_vector_desensitize.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    from pymilvus import connections, utility, Collection
    from app.config import settings
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeEntry, KnowledgeStatus
    from app.services.vector_service import (
        get_collection,
        chunk_text,
        embed_texts,
        _desensitize_chunks_llm,
        COLLECTION_NAME,
    )

    # 1. 连接 Milvus
    logger.info("Connecting to Milvus...")
    connections.connect("default", host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)

    # 2. 检查旧 collection schema
    if utility.has_collection(COLLECTION_NAME):
        old_col = Collection(COLLECTION_NAME)
        old_fields = {f.name for f in old_col.schema.fields}
        if "created_by" in old_fields and "desensitized_text" in old_fields:
            # Schema is up-to-date; check if collection is empty
            old_col.load()
            count = old_col.query(expr="knowledge_id >= 0", output_fields=["knowledge_id"], limit=1)
            if count:
                logger.info("Schema already up-to-date and collection has data, skipping migration.")
                return
            logger.info("Schema up-to-date but collection is empty, proceeding with re-index.")

        logger.info(f"Dropping old collection '{COLLECTION_NAME}' (missing new fields)...")
        old_col.drop()
        logger.info("Old collection dropped.")

    # 3. 创建新 collection（通过 get_collection 自动建）
    logger.info("Creating new collection with updated schema...")
    col = get_collection()
    logger.info("New collection created.")

    # 4. 从 MySQL 拉所有已审批知识重新入库
    db = SessionLocal()
    try:
        entries = (
            db.query(KnowledgeEntry)
            .filter(KnowledgeEntry.status == KnowledgeStatus.APPROVED)
            .all()
        )
        logger.info(f"Found {len(entries)} approved knowledge entries to re-index.")

        for i, entry in enumerate(entries):
            if not entry.content:
                continue
            try:
                chunks = chunk_text(entry.content)
                embeddings = embed_texts(chunks)
                desensitized = _desensitize_chunks_llm(chunks)

                col.insert([
                    [entry.id] * len(chunks),
                    list(range(len(chunks))),
                    [entry.created_by or 0] * len(chunks),
                    chunks,
                    desensitized,
                    embeddings,
                ])

                if (i + 1) % 10 == 0:
                    col.flush()
                    logger.info(f"  Progress: {i + 1}/{len(entries)}")

            except Exception as e:
                logger.warning(f"  Failed entry {entry.id} ({entry.title}): {e}")

        col.flush()
        logger.info("Migration complete.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
