"""
知识文档编辑权限申请系统 — 端到端测试

测试场景：
1. employee 打开他人文档 → 无编辑权限
2. employee 申请编辑权限 → 创建 ApprovalRequest
3. 重复申请 → 拒绝
4. 文档创建者查看 incoming 审批 → 看到申请
5. 文档创建者通过审批 → 自动写入 KnowledgeEditGrant
6. employee 再次检查 → 有编辑权限
7. employee 编辑文档（PATCH）→ 成功
8. 创建者撤销权限 → grant 删除
9. employee 再次检查 → 无权限
10. dept_admin 打开非自己文档 → 也无权限，需要申请
11. super_admin → 始终有权限
12. 文档创建者 → 始终有权限
"""

"""
需要在 venv 中运行：
  cd backend && source .venv/bin/activate && python test_edit_permission.py
或使用 curl 方式运行（不依赖 requests 库版本）。
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://localhost:8000/api"

# ── 工具函数 ────────────────────────────────────────────────────────────────

def token_for(user_id: int, role: str) -> str:
    """直接生成 JWT，不走登录流程（避免 bcrypt 依赖问题）"""
    import datetime
    from jose import jwt
    secret = "change-me-in-production"
    exp = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    return jwt.encode({"sub": str(user_id), "role": role, "exp": exp}, secret, algorithm="HS256")


class SimpleResponse:
    """简单封装 urllib 响应，避免依赖 requests 库版本问题。"""
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


class requests:
    """使用 urllib 实现的最小 requests 替代。"""
    # 绕过系统代理，直连 localhost
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _do(method: str, url: str, headers: dict = None, json_data=None) -> SimpleResponse:
        data = None
        hdrs = dict(headers or {})
        if json_data is not None:
            data = json.dumps(json_data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with requests._opener.open(req) as resp:
                return SimpleResponse(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            return SimpleResponse(e.code, e.read())

    @staticmethod
    def get(url, headers=None):
        return requests._do("GET", url, headers)

    @staticmethod
    def post(url, json=None, headers=None):
        return requests._do("POST", url, headers, json)

    @staticmethod
    def patch(url, json=None, headers=None):
        return requests._do("PATCH", url, headers, json)

    @staticmethod
    def delete(url, headers=None):
        return requests._do("DELETE", url, headers)

def auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}

def ok(resp, label: str):
    if resp.ok:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label} [{resp.status_code}] {resp.text[:200]}")
    return resp

def expect_status(resp, code: int, label: str):
    if resp.status_code == code:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label} — 期望 {code}，实际 {resp.status_code}: {resp.text[:200]}")
    return resp

# ── 准备测试数据 ─────────────────────────────────────────────────────────────

# 用户：
#   id=5 茹露容 employee dept=17  — 申请者
#   id=4 廖夏   super_admin dept=17 — 文档创建者（也是超管）
#   id=3 胡立琼 dept_admin dept=17
#   id=7 吕静   employee dept=18

employee_tok   = token_for(5, "employee")       # 茹露容
creator_tok    = token_for(4, "super_admin")     # 廖夏（创建者）
dept_admin_tok = token_for(3, "dept_admin")      # 胡立琼
other_emp_tok  = token_for(7, "employee")        # 吕静（跨部门）
super_tok      = token_for(1, "super_admin")     # 超级管理员

print("=" * 60)
print("知识文档编辑权限申请系统 — 端到端测试")
print("=" * 60)

# ── Step 0: 创建测试文档 ────────────────────────────────────────────────────

print("\n[Step 0] 创建测试文档（廖夏创建）")
resp = requests.post(f"{BASE}/knowledge", json={
    "title": "测试编辑权限文档",
    "content": "这是一篇用于测试编辑权限的文档内容。",
    "category": "experience",
}, headers=auth(creator_tok))
ok(resp, "创建文档")
entry = resp.json()
entry_id = entry["id"]
print(f"  → 文档 ID: {entry_id}")

# ── Step 1: employee 检查权限 → 无编辑权限 ─────────────────────────────────

print("\n[Step 1] employee（茹露容）检查编辑权限 → 无权限")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-permission", headers=auth(employee_tok))
ok(resp, "检查权限")
data = resp.json()
assert data["can_edit"] == False, f"期望 can_edit=False，实际 {data['can_edit']}"
assert data["pending_request"] is None, "期望无 pending_request"
print(f"  → can_edit={data['can_edit']}, pending_request={data['pending_request']}")

# ── Step 2: employee 申请编辑权限 ──────────────────────────────────────────

print("\n[Step 2] employee 申请编辑权限")
resp = requests.post(f"{BASE}/knowledge/{entry_id}/request-edit", headers=auth(employee_tok))
ok(resp, "申请编辑权限")
req_data = resp.json()
request_id = req_data["id"]
print(f"  → ApprovalRequest ID: {request_id}, status: {req_data['status']}")

# ── Step 3: 重复申请 → 400 ────────────────────────────────────────────────

print("\n[Step 3] 重复申请 → 拒绝")
resp = requests.post(f"{BASE}/knowledge/{entry_id}/request-edit", headers=auth(employee_tok))
expect_status(resp, 400, "重复申请被拒绝")

# ── Step 4: 检查权限有 pending_request ────────────────────────────────────

print("\n[Step 4] employee 检查权限 → 有 pending_request")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-permission", headers=auth(employee_tok))
ok(resp, "检查权限")
data = resp.json()
assert data["can_edit"] == False
assert data["pending_request"] is not None
print(f"  → pending_request id={data['pending_request']['id']}")

# ── Step 5: 文档创建者查看 incoming ───────────────────────────────────────

print("\n[Step 5] 文档创建者（廖夏）查看 incoming 审批")
resp = requests.get(f"{BASE}/approvals/incoming", headers=auth(creator_tok))
ok(resp, "获取 incoming")
items = resp.json()
matched = [i for i in items if i["id"] == request_id]
assert len(matched) == 1, f"期望在 incoming 中找到 request {request_id}"
print(f"  → 找到 {len(matched)} 条匹配审批")

# ── Step 6: pending-count 包含该审批 ──────────────────────────────────────

print("\n[Step 6] pending-count 包含该审批")
resp = requests.get(f"{BASE}/approvals/pending-count", headers=auth(creator_tok))
ok(resp, "获取 pending-count")
count = resp.json()["count"]
assert count >= 1, f"期望 count >= 1，实际 {count}"
print(f"  → pending count: {count}")

# ── Step 7: 文档创建者通过审批 ────────────────────────────────────────────

print("\n[Step 7] 文档创建者通过审批")
resp = requests.post(f"{BASE}/approvals/{request_id}/actions", json={
    "action": "approve",
}, headers=auth(creator_tok))
ok(resp, "审批通过")

# ── Step 8: employee 再次检查 → 有编辑权限 ────────────────────────────────

print("\n[Step 8] employee 检查权限 → 有编辑权限")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-permission", headers=auth(employee_tok))
ok(resp, "检查权限")
data = resp.json()
assert data["can_edit"] == True, f"期望 can_edit=True，实际 {data['can_edit']}"
print(f"  → can_edit={data['can_edit']}")

# ── Step 9: employee 编辑文档 → 成功 ─────────────────────────────────────

print("\n[Step 9] employee 编辑文档（PATCH）")
resp = requests.patch(f"{BASE}/knowledge/{entry_id}", json={
    "content": "编辑后的内容。",
    "content_html": "<p>编辑后的内容。</p>",
}, headers=auth(employee_tok))
ok(resp, "PATCH 编辑成功")

# ── Step 10: 创建者查看 grants 列表 ──────────────────────────────────────

print("\n[Step 10] 创建者查看 edit-grants 列表")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-grants", headers=auth(creator_tok))
ok(resp, "获取 grants")
grants = resp.json()
assert any(g["user_id"] == 5 for g in grants), "期望找到 user_id=5 的 grant"
print(f"  → {len(grants)} 个授权: {[g['user_name'] for g in grants]}")

# ── Step 11: 创建者撤销权限 ──────────────────────────────────────────────

print("\n[Step 11] 创建者撤销 employee 编辑权限")
resp = requests.delete(f"{BASE}/knowledge/{entry_id}/edit-grants/5", headers=auth(creator_tok))
ok(resp, "撤销权限")

# ── Step 12: employee 再次检查 → 无权限 ──────────────────────────────────

print("\n[Step 12] employee 检查权限 → 无权限")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-permission", headers=auth(employee_tok))
ok(resp, "检查权限")
data = resp.json()
assert data["can_edit"] == False, f"期望 can_edit=False，实际 {data['can_edit']}"
print(f"  → can_edit={data['can_edit']}")

# ── Step 13: employee PATCH → 403 ───────────────────────────────────────

print("\n[Step 13] employee PATCH → 403 无权限")
resp = requests.patch(f"{BASE}/knowledge/{entry_id}", json={
    "content": "不应该成功的编辑。",
}, headers=auth(employee_tok))
expect_status(resp, 403, "PATCH 被拒绝")

# ── Step 14: dept_admin 检查权限 → 也无权限 ──────────────────────────────

print("\n[Step 14] dept_admin（胡立琼）检查他人文档权限 → 无权限")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-permission", headers=auth(dept_admin_tok))
ok(resp, "检查权限")
data = resp.json()
assert data["can_edit"] == False, f"dept_admin 也应无权限，实际 {data['can_edit']}"
print(f"  → can_edit={data['can_edit']}")

# ── Step 15: super_admin 始终有权限 ──────────────────────────────────────

print("\n[Step 15] super_admin（超级管理员）检查权限 → 有权限")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-permission", headers=auth(super_tok))
ok(resp, "检查权限")
data = resp.json()
assert data["can_edit"] == True, f"super_admin 应有权限，实际 {data['can_edit']}"
print(f"  → can_edit={data['can_edit']}")

# ── Step 16: 文档创建者始终有权限 ────────────────────────────────────────

print("\n[Step 16] 文档创建者检查权限 → 有权限")
resp = requests.get(f"{BASE}/knowledge/{entry_id}/edit-permission", headers=auth(creator_tok))
ok(resp, "检查权限")
data = resp.json()
assert data["can_edit"] == True
assert data["is_owner"] == True
print(f"  → can_edit={data['can_edit']}, is_owner={data['is_owner']}")

# ── Step 17: 我发起的审批（employee 视角） ───────────────────────────────

print("\n[Step 17] employee 查看「我发起的」审批")
resp = requests.get(f"{BASE}/approvals/my", headers=auth(employee_tok))
ok(resp, "获取 my approvals")
my_items = resp.json()
matched = [i for i in my_items if i["id"] == request_id]
assert len(matched) == 1
assert matched[0]["status"] == "approved"
print(f"  → 找到已通过的审批 id={request_id}")

# ── 清理 ─────────────────────────────────────────────────────────────────

print("\n[清理] 删除测试文档")
resp = requests.delete(f"{BASE}/knowledge/{entry_id}", headers=auth(creator_tok))
if resp.ok:
    print(f"  ✓ 已删除文档 {entry_id}")
else:
    print(f"  ⚠ 删除失败 [{resp.status_code}] — 需手动清理")

print("\n" + "=" * 60)
print("测试完成！")
print("=" * 60)
