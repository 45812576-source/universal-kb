"""导出 Skill Studio rollout 指标。

用法:
    cd backend && python scripts/export_studio_rollout_metrics.py
    cd backend && python scripts/export_studio_rollout_metrics.py --days 14 --limit 1000 --format csv
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.services.studio_rollout_dashboard import (
    build_studio_rollout_dashboard,
    export_studio_rollout_dashboard_csv,
)


def _arg_value(flag: str, default: str) -> str:
    if flag not in sys.argv:
        return default
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except IndexError:
        return default


def main() -> None:
    days = int(_arg_value("--days", "7"))
    limit = int(_arg_value("--limit", "500"))
    export_format = _arg_value("--format", "json").lower()

    db = SessionLocal()
    try:
        if export_format == "csv":
            print(export_studio_rollout_dashboard_csv(db, since_days=days, limit=limit))
            return

        payload = build_studio_rollout_dashboard(db, since_days=days, limit=limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
