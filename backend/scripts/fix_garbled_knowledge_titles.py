"""一次性修复历史知识标题乱码。

仅修复高置信度场景：
1. title 明显乱码
2. source_file 可恢复出更合理中文标题
"""
from __future__ import annotations

import os
import re

from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry
from app.routers.knowledge import _sanitize_title


def _looks_garbled(text: str | None) -> bool:
    if not text:
        return False
    return any(mark in text for mark in ("Ã", "Â", "å", "æ", "ä", "�"))


def main() -> None:
    db = SessionLocal()
    updated = 0
    try:
        entries = db.query(KnowledgeEntry).all()
        for entry in entries:
            if not _looks_garbled(entry.title):
                continue

            candidates = []
            if entry.source_file:
                candidates.append(_sanitize_title(entry.source_file))
            if entry.title:
                candidates.append(_sanitize_title(entry.title))

            replacement = next((c for c in candidates if c and c != "未命名文档" and not _looks_garbled(c)), None)
            if not replacement:
                continue

            entry.title = replacement
            updated += 1
            print(f"FIX {entry.id}: {replacement}")

        db.commit()
        print(f"UPDATED {updated}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
