"""全员用户权限初始化
基于花名册CSV，创建全部用户账号、部门、岗位映射、角色分配。

规则：
- 用户名格式：名_姓（拼音），如 廖夏 → xia_liao
- 密码：8位随机大小写字母+数字+符号
- 超级管理员：xia_liao, zuchen_zhang
- 部门管理员：每个部门（有二级部门以二级部门为准）中，M线最高职级 > S线最高职级，最高者为 dept_admin
- 其他人：employee

运行方式（从 backend 目录）：
  conda run -n base python scripts/seed_users.py
"""
import csv
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pypinyin import Style, pinyin

from app.database import SessionLocal
from app.models.permission import Position
from app.models.user import Department, Role, User

import bcrypt as _bcrypt


class _BcryptHelper:
    @staticmethod
    def hash(password: str) -> str:
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


bcrypt = _BcryptHelper()

# ─── 配置 ────────────────────────────────────────────────────────────────────
CSV_PATH = os.path.join(
    os.path.dirname(__file__),
    "employee_roster_utf8.csv",
)

SUPER_ADMINS = {"xia_liao", "zuchen_zhang"}

# 手动角色覆盖（优先级最高，覆盖自动推断）
ROLE_OVERRIDES = {
    "liqiong_hu": Role.DEPT_ADMIN,       # DIC总负责人，最高管理权限
    "jianqiang_lian": Role.EMPLOYEE,     # 厦门运营部普通员工
    "sha_ceng": Role.EMPLOYEE,           # 创意组普通员工
    "qianyi_ceng": Role.EMPLOYEE,        # DIC产品部普通员工
    "huanyu_huang": Role.EMPLOYEE,       # 商城运营部普通员工
}

# 岗位覆盖（liqiong_hu 作为DIC总负责人应为管理层）
POSITION_OVERRIDES = {
    "liqiong_hu": "管理层",
}

# 管辖部门覆盖（username -> 管辖部门名）
# 默认：dept_admin 管辖自己所在部门及其子部门
# 特殊：胡立琼管辖整个 AI云浏览器事业部
MANAGED_DEPT_OVERRIDES = {
    "liqiong_hu": "AI云浏览器事业部",
}

# 职级排序：M线 > S线 > A线，同线内数字越大越高
LEVEL_ORDER = {
    "M6": 60, "M5": 50, "M4": 40, "M3": 30, "M2": 20, "M1": 10, "M0": 5,
    "S3": 4, "S2": 3, "S1": 2,
    "A1": 1,
}

# 岗位映射：根据花名册中的岗位名、部门名，映射到权限系统的9个岗位
# 9岗位：商务/媒介/运营/创意/产研/客户成功/财务/HR/管理层
POSITION_MAPPING_RULES = [
    # (匹配条件, 权限岗位) — 优先级从高到低
    # 管理层：M4+
    (lambda row: row["职级"] in ("M4", "M5", "M6"), "管理层"),
    # 财务
    (lambda row: "财务" in row["岗位"] or "财务" in row["一级部门"], "财务"),
    # HR/行政
    (lambda row: any(k in row["岗位"] for k in ("HRBP", "招聘", "行政", "人力")), "HR"),
    (lambda row: "人力资源" in row["一级部门"] or "行政" in row["一级部门"], "HR"),
    (lambda row: "助理" in row["岗位"], "HR"),
    # 媒介 — 媒介服务部
    (lambda row: "媒介" in row["岗位"], "媒介"),
    (lambda row: "媒介服务" in row.get("二级部门", ""), "媒介"),
    # 创意 — 编导/摄影/剪辑/创意组
    (lambda row: any(k in row["岗位"] for k in ("编导", "摄影", "剪辑", "创意")), "创意"),
    (lambda row: "创意组" in row.get("二级部门", ""), "创意"),
    # 客户成功 — 技术产品交付部
    (lambda row: any(k in row["岗位"] for k in ("交付", "项目管理")), "客户成功"),
    (lambda row: "技术产品交付" in row.get("二级部门", ""), "客户成功"),
    # 商务 — 客户经理/商务拓展/销售
    (lambda row: any(k in row["岗位"] for k in ("客户经理", "商务", "销售")), "商务"),
    (lambda row: any(k in row.get("二级部门", "") for k in ("商务拓展", "客户管理")), "商务"),
    # 运营 — 投流运营/产品运营/市场运营/店铺运营
    (lambda row: any(k in row["岗位"] for k in ("运营", "店铺运营", "市场", "营销")), "运营"),
    (lambda row: any(k in row.get("二级部门", "") for k in ("运营",)), "运营"),
    (lambda row: any(k in row["一级部门"] for k in ("电商投流运营", "商城运营")), "运营"),
    (lambda row: "DIC商业化" in row["一级部门"], "运营"),
    # 产研 — 开发/测试/产品/UI/设计
    (lambda row: any(k in row["岗位"] for k in ("开发", "工程师", "产品", "UI", "设计", "测试")), "产研"),
    (lambda row: "产研" in row["一级部门"], "产研"),
]


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def chinese_to_pinyin_username(name: str) -> str:
    """中文姓名 → 名_姓 拼音格式"""
    # 常见复姓
    compound_surnames = {"欧阳", "司马", "上官", "诸葛", "令狐", "皇甫"}
    surname = ""
    given = ""
    for cs in compound_surnames:
        if name.startswith(cs):
            surname = cs
            given = name[len(cs):]
            break
    if not surname:
        surname = name[0]
        given = name[1:]

    s_py = "".join([p[0] for p in pinyin(surname, style=Style.NORMAL)])
    g_py = "".join([p[0] for p in pinyin(given, style=Style.NORMAL)])
    return f"{g_py}_{s_py}"


def generate_password(length: int = 8) -> str:
    """生成8位随机密码：至少1大写+1小写+1数字+1符号"""
    upper = secrets.choice(string.ascii_uppercase)
    lower = secrets.choice(string.ascii_lowercase)
    digit = secrets.choice(string.digits)
    symbol = secrets.choice("!@#$%^&*")
    rest = [secrets.choice(string.ascii_letters + string.digits + "!@#$%^&*")
            for _ in range(length - 4)]
    pool = list(upper + lower + digit + symbol) + rest
    secrets.SystemRandom().shuffle(pool)
    return "".join(pool)


def determine_position(row: dict) -> str:
    """根据花名册信息确定权限岗位"""
    for rule_fn, pos_name in POSITION_MAPPING_RULES:
        if rule_fn(row):
            return pos_name
    return "策划"  # 默认


def get_dept_key(row: dict) -> str:
    """确定部门key：有二级部门用二级，否则用一级。DIC产研下属二级部门加前缀区分。"""
    d2 = row["二级部门"].strip()
    d1 = row["一级部门"].strip()
    bu = row["事业部门"].strip()
    if d2 and d2 != "无":
        # DIC产研的二级部门与CID产研重名，数据库中加了DIC前缀
        if d1 == "DIC产研" and d2 in ("前端研发部", "后端研发部", "产品部", "测试部"):
            return f"DIC{d2}"
        return d2
    return d1


# ─── 主流程 ────────────────────────────────────────────────────────────────────

def seed_users():
    # 1. 读取CSV
    if not os.path.exists(CSV_PATH):
        # 尝试从GBK转换
        gbk_path = os.path.expanduser(
            "~/Downloads/天机-人力资源管理工作模块_员工花名册及人事档案_表格(1).csv"
        )
        if os.path.exists(gbk_path):
            import codecs
            with codecs.open(gbk_path, "r", "gbk") as fin, \
                 open(CSV_PATH, "w", encoding="utf-8") as fout:
                fout.write(fin.read())
            print(f"已将 GBK CSV 转换为 UTF-8: {CSV_PATH}")
        else:
            print(f"找不到花名册文件: {CSV_PATH} 或 {gbk_path}")
            return

    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"读取花名册 {len(rows)} 人")

    # 清理字段名空格
    cleaned_rows = []
    for row in rows:
        cleaned_rows.append({k.strip(): v.strip() for k, v in row.items()})
    rows = cleaned_rows

    db = SessionLocal()
    try:
        # 幂等检查
        if db.query(User).filter(User.username == "xia_liao").first():
            print("全员用户数据已存在，跳过。如需重跑请先清空 users 表。")
            db.close()
            return

        # 2. 确保部门结构存在
        print("检查/创建部门结构...")
        dept_cache = {}  # name -> Department
        for d in db.query(Department).all():
            dept_cache[d.name] = d

        # 创建事业部（一级）
        bus = set()
        for r in rows:
            bus.add((r["类别"], r["事业部门"]))
        for cat, bu_name in bus:
            if bu_name not in dept_cache:
                d = Department(name=bu_name, category=cat, business_unit=bu_name)
                db.add(d)
                db.flush()
                dept_cache[bu_name] = d

        # 创建一级部门
        d1s = set()
        for r in rows:
            d1s.add((r["类别"], r["事业部门"], r["一级部门"]))
        for cat, bu_name, d1_name in d1s:
            if d1_name not in dept_cache:
                parent = dept_cache.get(bu_name)
                d = Department(
                    name=d1_name, category=cat,
                    business_unit=bu_name,
                    parent_id=parent.id if parent else None,
                )
                db.add(d)
                db.flush()
                dept_cache[d1_name] = d

        # 创建二级部门
        d2s = set()
        for r in rows:
            d2 = r["二级部门"]
            if d2 and d2 != "无":
                d2s.add((r["类别"], r["事业部门"], r["一级部门"], d2))
        for cat, bu_name, d1_name, d2_name in d2s:
            # DIC产研的二级部门与CID产研重名，加DIC前缀
            db_name = d2_name
            if d1_name == "DIC产研" and d2_name in ("前端研发部", "后端研发部", "产品部", "测试部"):
                db_name = f"DIC{d2_name}"
            if db_name not in dept_cache:
                parent = dept_cache.get(d1_name)
                d = Department(
                    name=db_name, category=cat,
                    business_unit=bu_name,
                    parent_id=parent.id if parent else None,
                )
                db.add(d)
                db.flush()
                dept_cache[db_name] = d

        db.flush()
        print(f"  部门总数: {len(dept_cache)}")

        # 3. 确保岗位存在
        pos_cache = {}
        for p in db.query(Position).all():
            pos_cache[p.name] = p
        for pname in ["商务", "策划", "财务", "HR", "管理层"]:
            if pname not in pos_cache:
                print(f"  [警告] 岗位 '{pname}' 不存在，请先运行 seed_permissions.py")
                return
        print(f"  岗位检查通过（{len(pos_cache)}个）")

        # 4. 确定各部门管理员
        # key=dept_key, value=(name, level_score)
        dept_top = {}
        for r in rows:
            dk = get_dept_key(r)
            lv = r["职级"]
            score = LEVEL_ORDER.get(lv, 0)
            name = r["人员名称"]
            if dk not in dept_top or score > dept_top[dk][1]:
                dept_top[dk] = (name, score)
        dept_admin_names = {v[0] for v in dept_top.values()}
        print(f"  部门管理员: {len(dept_admin_names)} 人")

        # 5. 创建用户
        print("创建用户...")
        # 停用旧的默认admin（有外键引用不能删除）
        old_admin = db.query(User).filter(User.username == "admin").first()
        if old_admin:
            old_admin.is_active = False
            db.flush()
            print("  已停用默认 admin 账号")

        credentials = []  # (display_name, username, password, role)
        username_seen = {}  # 处理重名

        for r in rows:
            name = r["人员名称"]
            username = chinese_to_pinyin_username(name)

            # 处理重名
            if username in username_seen:
                username_seen[username] += 1
                username = f"{username}{username_seen[username]}"
            else:
                username_seen[username] = 1

            password = generate_password()

            # 确定角色（手动覆盖 > 超管 > 自动推断）
            if username in ROLE_OVERRIDES:
                role = ROLE_OVERRIDES[username]
            elif username in SUPER_ADMINS:
                role = Role.SUPER_ADMIN
            elif name in dept_admin_names:
                role = Role.DEPT_ADMIN
            else:
                role = Role.EMPLOYEE

            # 确定部门
            dk = get_dept_key(r)
            dept = dept_cache.get(dk)

            # 确定权限岗位（手动覆盖 > 自动推断）
            if username in POSITION_OVERRIDES:
                pos_name = POSITION_OVERRIDES[username]
            else:
                pos_name = determine_position(r)
            pos = pos_cache.get(pos_name)

            # 确定管辖部门（dept_admin 默认管辖所在部门，可覆盖）
            managed_dept_id = None
            if role == Role.DEPT_ADMIN:
                if username in MANAGED_DEPT_OVERRIDES:
                    md = dept_cache.get(MANAGED_DEPT_OVERRIDES[username])
                    managed_dept_id = md.id if md else None
                elif dept:
                    managed_dept_id = dept.id

            user = User(
                username=username,
                password_hash=bcrypt.hash(password),
                display_name=name,
                role=role,
                department_id=dept.id if dept else None,
                managed_department_id=managed_dept_id,
                position_id=pos.id if pos else None,
                is_active=True,
            )
            db.add(user)
            credentials.append((name, username, password, role.value, pos_name, dk))

        db.flush()

        # 5.5 写入直属上级（report_to_id）
        print("写入直属上级关系...")
        # 花名册中"直属上级"用的是简称，需要处理别名
        SUPERIOR_ALIASES = {
            "曾珠": "曾小珠",
        }
        user_by_display = {}
        for u in db.query(User).filter(User.is_active == True).all():
            user_by_display[u.display_name] = u

        report_count = 0
        report_missing = []
        for r in rows:
            name = r["人员名称"]
            superior_name = r.get("直属上级", "").strip()
            if not superior_name:
                continue
            # 别名转换
            superior_name = SUPERIOR_ALIASES.get(superior_name, superior_name)
            user = user_by_display.get(name)
            superior = user_by_display.get(superior_name)
            if user and superior:
                user.report_to_id = superior.id
                report_count += 1
            elif user and not superior:
                report_missing.append((name, superior_name))

        if report_missing:
            print(f"  [警告] {len(report_missing)} 人的直属上级未找到:")
            for n, s in report_missing:
                print(f"    {n} → {s}")
        print(f"  已设置 {report_count} 人的直属上级")

        db.flush()
        db.commit()

        # 6. 输出结果
        print(f"\n{'='*80}")
        print(f"全员用户创建完成！共 {len(credentials)} 人")
        print(f"{'='*80}")

        # 统计
        role_stats = {}
        pos_stats = {}
        for _, _, _, role_val, pos_name, _ in credentials:
            role_stats[role_val] = role_stats.get(role_val, 0) + 1
            pos_stats[pos_name] = pos_stats.get(pos_name, 0) + 1

        print("\n角色分布:")
        for k, v in sorted(role_stats.items()):
            print(f"  {k}: {v}")
        print("\n岗位分布:")
        for k, v in sorted(pos_stats.items()):
            print(f"  {k}: {v}")

        # 输出密码表（CSV）
        pwd_file = os.path.join(os.path.dirname(__file__), "user_credentials.csv")
        name_to_superior = {}
        for r in rows:
            sup = r.get("直属上级", "").strip()
            sup = SUPERIOR_ALIASES.get(sup, sup)
            name_to_superior[r["人员名称"]] = sup
        with open(pwd_file, "w", encoding="utf-8") as f:
            f.write("姓名,用户名,密码,系统角色,权限岗位,部门,直属上级\n")
            for name, uname, pwd, role_val, pos_name, dk in credentials:
                sup = name_to_superior.get(name, "")
                f.write(f"{name},{uname},{pwd},{role_val},{pos_name},{dk},{sup}\n")
        print(f"\n密码表已导出: {pwd_file}")
        print("请妥善保管此文件，分发后建议删除。")

        # 打印超管和部门管理员
        print("\n超级管理员:")
        for name, uname, pwd, role_val, pos_name, dk in credentials:
            if role_val == "super_admin":
                print(f"  {name} | {uname} | {pwd}")

        print("\n部门管理员:")
        for name, uname, pwd, role_val, pos_name, dk in credentials:
            if role_val == "dept_admin":
                print(f"  {name} | {uname} | {pwd} | {dk}")

    except Exception as e:
        db.rollback()
        print(f"\n[ERROR] 创建失败: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_users()
