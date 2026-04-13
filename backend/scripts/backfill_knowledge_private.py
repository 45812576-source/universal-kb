"""将历史知识条目回收为 private，避免不同用户之间互相可见。

用法:
    cd backend && python scripts/backfill_knowledge_private.py
    cd backend && python scripts/backfill_knowledge_private.py --dry-run
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_

from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry


DRY_RUN = "--dry-run" in sys.argv


def main() -> None:
    db = SessionLocal()
    try:
        rows = (
            db.query(KnowledgeEntry)
            .filter(
                or_(
                    KnowledgeEntry.visibility_scope.is_(None),
                    KnowledgeEntry.visibility_scope != "private",
                )
            )
            .all()
        )

        print(f"命中 {len(rows)} 条历史知识")
        for entry in rows[:20]:
            print(
                f"  #{entry.id} user={entry.created_by} "
                f"status={entry.status} visibility={entry.visibility_scope!r} "
                f"title={((entry.title or '')[:40])!r}"
            )
        if len(rows) > 20:
            print(f"  ... 其余 {len(rows) - 20} 条省略")

        if DRY_RUN:
            print("\n[dry-run] 未写入数据库")
            return

        for entry in rows:
            entry.visibility_scope = "private"

        db.commit()
        print(f"\n✓ 已回收 {len(rows)} 条为 private")
    except Exception:
        import traceback

        traceback.print_exc()
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
