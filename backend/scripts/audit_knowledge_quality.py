"""知识库质量审查脚本。

输出三类风险：
1. 疑似乱码标题
2. 低置信度却已有 folder_id 的自动归档风险
3. taxonomy_code 与系统目录不一致
"""
from __future__ import annotations

from collections import defaultdict

from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry, KnowledgeFolder


def _looks_garbled(text: str | None) -> bool:
    if not text:
        return False
    markers = ("Ã", "Â", "å", "æ", "ä", "�")
    return any(mark in text for mark in markers)


def main() -> None:
    db = SessionLocal()
    try:
        folders = {f.id: f for f in db.query(KnowledgeFolder).all()}
        garbled = []
        low_conf_filed = []
        taxonomy_mismatch = []

        for entry in db.query(KnowledgeEntry).all():
            if _looks_garbled(entry.title) or _looks_garbled(entry.source_file):
                garbled.append((entry.id, entry.title, entry.source_file))

            if (
                entry.folder_id is not None
                and entry.classification_confidence is not None
                and entry.classification_confidence < 0.6
            ):
                low_conf_filed.append(
                    (entry.id, entry.title, entry.folder_id, entry.classification_confidence, entry.taxonomy_code, entry.taxonomy_board)
                )

            if entry.folder_id and entry.taxonomy_code:
                folder = folders.get(entry.folder_id)
                if folder and folder.is_system == 1 and folder.taxonomy_code and folder.taxonomy_code != entry.taxonomy_code:
                    taxonomy_mismatch.append(
                        (entry.id, entry.title, entry.taxonomy_code, folder.taxonomy_code, folder.name)
                    )

        print("=== GARBLED TITLES ===")
        for row in garbled[:200]:
            print(row)

        print("\n=== LOW CONFIDENCE FILED ===")
        for row in low_conf_filed[:200]:
            print(row)

        print("\n=== TAXONOMY MISMATCH ===")
        for row in taxonomy_mismatch[:200]:
            print(row)

        print(
            "\nSUMMARY",
            {
                "garbled": len(garbled),
                "low_confidence_filed": len(low_conf_filed),
                "taxonomy_mismatch": len(taxonomy_mismatch),
            },
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
