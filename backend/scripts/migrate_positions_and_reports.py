"""迁移脚本：更新用户权限岗位 + 写入直属上级关系
1. ALTER TABLE 添加 report_to_id 列
2. 按新的9岗位规则更新 position_id（消除旧的"策划"映射）
3. 根据花名册的"直属上级"字段写入 report_to_id
4. 重新生成 user_credentials.csv 到桌面

运行方式（从 backend 目录）：
  conda run -n base python scripts/migrate_positions_and_reports.py
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import SessionLocal
from app.models.permission import Position
from app.models.user import User

# ─── 岗位映射规则（9岗位，无"策划"）─────────────────────────────────────
POSITION_MAPPING_RULES = [
    (lambda row: row["职级"] in ("M4", "M5", "M6"), "管理层"),
    (lambda row: "财务" in row["岗位"] or "财务" in row["一级部门"], "财务"),
    (lambda row: any(k in row["岗位"] for k in ("HRBP", "招聘", "行政", "人力")), "HR"),
    (lambda row: "人力资源" in row["一级部门"] or "行政" in row["一级部门"], "HR"),
    (lambda row: "助理" in row["岗位"], "HR"),
    (lambda row: "媒介" in row["岗位"], "媒介"),
    (lambda row: "媒介服务" in row.get("二级部门", ""), "媒介"),
    (lambda row: any(k in row["岗位"] for k in ("编导", "摄影", "剪辑", "创意")), "创意"),
    (lambda row: "创意组" in row.get("二级部门", ""), "创意"),
    (lambda row: any(k in row["岗位"] for k in ("交付", "项目管理")), "客户成功"),
    (lambda row: "技术产品交付" in row.get("二级部门", ""), "客户成功"),
    (lambda row: any(k in row["岗位"] for k in ("客户经理", "商务", "销售")), "商务"),
    (lambda row: any(k in row.get("二级部门", "") for k in ("商务拓展", "客户管理")), "商务"),
    (lambda row: any(k in row["岗位"] for k in ("运营", "店铺运营", "市场", "营销")), "运营"),
    (lambda row: any(k in row.get("二级部门", "") for k in ("运营",)), "运营"),
    (lambda row: any(k in row["一级部门"] for k in ("电商投流运营", "商城运营")), "运营"),
    (lambda row: "DIC商业化" in row["一级部门"], "运营"),
    (lambda row: any(k in row["岗位"] for k in ("开发", "工程师", "产品", "UI", "设计", "测试")), "产研"),
    (lambda row: "产研" in row["一级部门"], "产研"),
]

POSITION_OVERRIDES = {
    "liqiong_hu": "管理层",
}


def determine_position(row: dict) -> str:
    for rule_fn, pos_name in POSITION_MAPPING_RULES:
        if rule_fn(row):
            return pos_name
    return "运营"  # fallback


def main():
    CSV_PATH = os.path.join(os.path.dirname(__file__), "employee_roster_utf8.csv")
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [{k.strip(): v.strip() for k, v in r.items()} for r in reader]
    print(f"读取花名册 {len(rows)} 人")

    db = SessionLocal()
    try:
        # ① ALTER TABLE 添加 report_to_id（幂等）
        try:
            db.execute(text(
                "ALTER TABLE users ADD COLUMN report_to_id INT NULL, "
                "ADD CONSTRAINT fk_users_report_to FOREIGN KEY (report_to_id) REFERENCES users(id)"
            ))
            db.commit()
            print("已添加 report_to_id 列")
        except Exception:
            db.rollback()
            print("report_to_id 列已存在，跳过 ALTER")

        # ② 加载岗位和用户缓存
        pos_cache = {p.name: p for p in db.query(Position).all()}
        print(f"岗位: {list(pos_cache.keys())}")

        # 删除旧的"策划"岗位（如存在且无引用）
        if "策划" in pos_cache:
            print("注意: '策划' 岗位存在，将在更新完 position_id 后尝试删除")

        user_by_username = {u.username: u for u in db.query(User).all()}
        user_by_display = {}  # display_name -> User（处理直属上级匹配）
        for u in user_by_username.values():
            user_by_display[u.display_name] = u

        # ③ 构建花名册 display_name -> row 映射
        name_to_row = {r["人员名称"]: r for r in rows}

        # ④ 更新 position_id
        updated_pos = 0
        for u in user_by_username.values():
            if u.display_name not in name_to_row:
                continue
            row = name_to_row[u.display_name]

            # 确定岗位
            if u.username in POSITION_OVERRIDES:
                pos_name = POSITION_OVERRIDES[u.username]
            else:
                pos_name = determine_position(row)

            pos = pos_cache.get(pos_name)
            if pos and u.position_id != pos.id:
                old_pos = next((p.name for p in pos_cache.values() if p.id == u.position_id), "无")
                u.position_id = pos.id
                updated_pos += 1
                print(f"  {u.display_name}: {old_pos} → {pos_name}")

        print(f"岗位更新: {updated_pos} 人")

        # ⑤ 写入 report_to_id
        updated_report = 0
        for u in user_by_username.values():
            if u.display_name not in name_to_row:
                continue
            row = name_to_row[u.display_name]
            superior_name = row.get("直属上级", "").strip()
            if not superior_name:
                continue
            superior = user_by_display.get(superior_name)
            if superior:
                u.report_to_id = superior.id
                updated_report += 1
            else:
                print(f"  [警告] {u.display_name} 的直属上级 '{superior_name}' 未找到")

        print(f"直属上级更新: {updated_report} 人")

        db.commit()

        # ⑥ 尝试删除旧"策划"岗位
        if "策划" in pos_cache:
            remaining = db.query(User).filter(User.position_id == pos_cache["策划"].id).count()
            if remaining == 0:
                db.delete(pos_cache["策划"])
                db.commit()
                print("已删除旧的 '策划' 岗位")
            else:
                print(f"'策划' 岗位仍有 {remaining} 人引用，暂不删除")

        # ⑦ 重新生成 user_credentials.csv
        out_path = os.path.expanduser("~/Desktop/user_credentials.csv")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("姓名,用户名,花名册岗位,权限岗位,事业部门,一级部门,二级部门,职级,直属上级\n")
            for r in rows:
                name = r["人员名称"]
                u = user_by_display.get(name)
                if not u:
                    continue
                pos = next((p.name for p in pos_cache.values() if p.id == u.position_id), "未知")
                superior = db.query(User).get(u.report_to_id).display_name if u.report_to_id else ""
                f.write(f"{name},{u.username},{r['岗位']},{pos},"
                        f"{r['事业部门']},{r['一级部门']},{r['二级部门']},{r['职级']},{superior}\n")
        print(f"\n已输出到 {out_path}")

        # ⑧ 统计
        print("\n岗位分布:")
        for pos_name, pos in sorted(pos_cache.items()):
            cnt = db.query(User).filter(User.position_id == pos.id, User.is_active == True).count()
            if cnt > 0:
                print(f"  {pos_name}: {cnt}")

        no_report = db.query(User).filter(
            User.report_to_id == None, User.is_active == True, User.username != "admin"
        ).count()
        print(f"\n无直属上级: {no_report} 人（应为最高管理层）")

    except Exception as e:
        db.rollback()
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


if __name__ == "__main__":
    main()
