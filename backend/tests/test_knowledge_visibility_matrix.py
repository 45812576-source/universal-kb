from app.models.knowledge import KnowledgeEntry, KnowledgeStatus, ReviewStage
from app.models.knowledge import KnowledgeFolder
from app.models.user import Role
from tests.conftest import _auth, _login, _make_dept, _make_user


def _titles(resp):
    return [item["title"] for item in resp.json()]


def test_employee_can_see_own_pending_only(client, db):
    dept = _make_dept(db, "矩阵部门A")
    owner = _make_user(db, "matrix_emp_owner", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "matrix_emp_other", Role.EMPLOYEE, dept.id)
    db.add_all([
        KnowledgeEntry(title="自己的待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=owner.id, department_id=dept.id),
        KnowledgeEntry(title="别人的待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=other.id, department_id=dept.id),
        KnowledgeEntry(title="别人的已通过", content="x", category="experience", status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED, created_by=other.id, department_id=dept.id),
    ])
    db.commit()

    token = _login(client, "matrix_emp_owner")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "自己的待审" in titles
    assert "别人的待审" not in titles
    assert "别人的已通过" in titles


def test_dept_admin_can_see_same_dept_pending_but_not_other_dept_pending(client, db):
    dept_a = _make_dept(db, "矩阵部门B")
    dept_b = _make_dept(db, "矩阵部门C")
    admin = _make_user(db, "matrix_admin", Role.DEPT_ADMIN, dept_a.id)
    same_dept = _make_user(db, "matrix_same", Role.EMPLOYEE, dept_a.id)
    other_dept = _make_user(db, "matrix_other", Role.EMPLOYEE, dept_b.id)
    db.add_all([
        KnowledgeEntry(title="同部门待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=same_dept.id, department_id=dept_a.id),
        KnowledgeEntry(title="外部门待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=other_dept.id, department_id=dept_b.id),
    ])
    db.commit()

    token = _login(client, "matrix_admin")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "同部门待审" in titles
    assert "外部门待审" not in titles


def test_super_admin_can_see_all_statuses(client, db):
    dept = _make_dept(db, "矩阵部门D")
    owner = _make_user(db, "matrix_owner", Role.EMPLOYEE, dept.id)
    sa = _make_user(db, "matrix_sa", Role.SUPER_ADMIN, dept.id)
    db.add_all([
        KnowledgeEntry(title="超管看待审", content="x", category="experience", status=KnowledgeStatus.PENDING, created_by=owner.id, department_id=dept.id),
        KnowledgeEntry(title="超管看通过", content="x", category="experience", status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED, created_by=owner.id, department_id=dept.id),
    ])
    db.commit()

    token = _login(client, "matrix_sa")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "超管看待审" in titles
    assert "超管看通过" in titles


def test_employee_list_visible_entry_detail_is_also_accessible(client, db):
    dept = _make_dept(db, "矩阵部门E")
    owner = _make_user(db, "matrix_emp_owner2", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "matrix_emp_other2", Role.EMPLOYEE, dept.id)
    approved = KnowledgeEntry(
        title="别人的已通过详情一致",
        content="x",
        category="experience",
        status=KnowledgeStatus.APPROVED,
        review_stage=ReviewStage.APPROVED,
        created_by=other.id,
        department_id=dept.id,
    )
    db.add_all([approved])
    db.commit()

    token = _login(client, "matrix_emp_owner2")
    list_resp = client.get("/api/knowledge", headers=_auth(token))
    assert list_resp.status_code == 200
    ids = [item["id"] for item in list_resp.json()]
    assert approved.id in ids

    detail_resp = client.get(f"/api/knowledge/{approved.id}", headers=_auth(token))
    assert detail_resp.status_code == 200


def test_list_folders_includes_visible_system_folder_for_entry(client, db):
    dept = _make_dept(db, "矩阵部门F")
    owner = _make_user(db, "matrix_emp_owner3", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "matrix_emp_other3", Role.EMPLOYEE, dept.id)
    system_folder = KnowledgeFolder(
        name="系统归档节点",
        parent_id=None,
        created_by=other.id,
        is_system=1,
    )
    db.add(system_folder)
    db.flush()
    approved = KnowledgeEntry(
        title="系统目录文档",
        content="x",
        category="experience",
        status=KnowledgeStatus.APPROVED,
        review_stage=ReviewStage.APPROVED,
        created_by=other.id,
        department_id=dept.id,
        folder_id=system_folder.id,
    )
    db.add(approved)
    db.commit()

    token = _login(client, "matrix_emp_owner3")
    resp = client.get("/api/knowledge/folders", headers=_auth(token))
    assert resp.status_code == 200
    folder_ids = {item["id"] for item in resp.json()}
    assert system_folder.id in folder_ids


def test_other_user_folder_not_leaked_as_own(client, db):
    """他人文件夹中有对我可见的已审批文档时，该文件夹不应标记为 own，
    前端"我的整理"视图据此过滤，防止泄漏他人目录结构。"""
    dept = _make_dept(db, "泄漏测试部门")
    me = _make_user(db, "leak_me", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "leak_other", Role.EMPLOYEE, dept.id)

    # 我自己的文件夹
    my_folder = KnowledgeFolder(name="我的笔记", parent_id=None, created_by=me.id)
    # 他人的文件夹
    other_folder = KnowledgeFolder(name="他的笔记", parent_id=None, created_by=other.id)
    db.add_all([my_folder, other_folder])
    db.flush()

    # 他人文件夹里有一篇已审批文档（对我可见）
    db.add(KnowledgeEntry(
        title="他人已审批文档", content="x", category="experience",
        status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED,
        created_by=other.id, department_id=dept.id, folder_id=other_folder.id,
    ))
    db.commit()

    token = _login(client, "leak_me")
    resp = client.get("/api/knowledge/folders", headers=_auth(token))
    assert resp.status_code == 200
    folders_by_id = {item["id"]: item for item in resp.json()}

    # 我自己的文件夹 → visibility=own
    assert my_folder.id in folders_by_id
    assert folders_by_id[my_folder.id]["visibility"] == "own"

    # 他人文件夹因可见文档被返回 → visibility=visible_doc（不是 own）
    assert other_folder.id in folders_by_id
    assert folders_by_id[other_folder.id]["visibility"] == "visible_doc"


def test_super_admin_other_folders_marked_visible_doc(client, db):
    """super_admin 能看到所有文档，但他人文件夹应标记为 visible_doc 而非 own。"""
    dept = _make_dept(db, "超管泄漏测试部门")
    admin = _make_user(db, "leak_admin", Role.SUPER_ADMIN, dept.id)
    other = _make_user(db, "leak_admin_other", Role.EMPLOYEE, dept.id)

    other_folder = KnowledgeFolder(name="员工笔记", parent_id=None, created_by=other.id)
    db.add(other_folder)
    db.flush()

    db.add(KnowledgeEntry(
        title="员工文档", content="x", category="experience",
        status=KnowledgeStatus.PENDING,
        created_by=other.id, department_id=dept.id, folder_id=other_folder.id,
    ))
    db.commit()

    token = _login(client, "leak_admin")
    resp = client.get("/api/knowledge/folders", headers=_auth(token))
    assert resp.status_code == 200
    folders_by_id = {item["id"]: item for item in resp.json()}

    assert other_folder.id in folders_by_id
    assert folders_by_id[other_folder.id]["visibility"] == "visible_doc"


def test_owner_only_returns_only_own_folders(client, db):
    """owner_only=true 模式只返回自己创建的文件夹，visibility 全部为 own。"""
    dept = _make_dept(db, "OwnerOnly测试部门")
    me = _make_user(db, "oo_me", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "oo_other", Role.EMPLOYEE, dept.id)

    my_f = KnowledgeFolder(name="我的", parent_id=None, created_by=me.id)
    other_f = KnowledgeFolder(name="他的", parent_id=None, created_by=other.id)
    db.add_all([my_f, other_f])
    db.commit()

    token = _login(client, "oo_me")
    resp = client.get("/api/knowledge/folders?owner_only=true", headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()
    ids = {item["id"] for item in items}
    assert my_f.id in ids
    assert other_f.id not in ids
    assert all(item["visibility"] == "own" for item in items)


# ---------- visibility_scope="private" (workdir_sync) ----------


def test_private_entry_invisible_to_other_employee(client, db):
    """private 条目即使 APPROVED，其他员工也看不到"""
    dept = _make_dept(db, "私有测试部门A")
    owner = _make_user(db, "priv_owner", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "priv_other", Role.EMPLOYEE, dept.id)
    db.add_all([
        KnowledgeEntry(title="私有开发工地", content="x", category="experience",
                       status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED,
                       created_by=owner.id, department_id=dept.id,
                       source_type="workdir_sync", visibility_scope="private"),
        KnowledgeEntry(title="普通已通过", content="x", category="experience",
                       status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED,
                       created_by=owner.id, department_id=dept.id),
    ])
    db.commit()

    # 其他员工看不到 private 条目
    token = _login(client, "priv_other")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "私有开发工地" not in titles
    assert "普通已通过" in titles


def test_private_entry_visible_to_creator(client, db):
    """创建者自己能看到自己的 private 条目"""
    dept = _make_dept(db, "私有测试部门B")
    owner = _make_user(db, "priv_creator", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(title="我的开发工地", content="x", category="experience",
                           status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED,
                           created_by=owner.id, department_id=dept.id,
                           source_type="workdir_sync", visibility_scope="private")
    db.add(entry)
    db.commit()

    token = _login(client, "priv_creator")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "我的开发工地" in titles

    # 详情也可访问
    detail = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
    assert detail.status_code == 200


def test_private_entry_invisible_to_dept_admin_of_other_user(client, db):
    """部门管理员也看不到别人的 private 条目（即使同部门）"""
    dept = _make_dept(db, "私有测试部门C")
    owner = _make_user(db, "priv_emp", Role.EMPLOYEE, dept.id)
    admin = _make_user(db, "priv_admin", Role.DEPT_ADMIN, dept.id)
    db.add(KnowledgeEntry(title="员工私有文档", content="x", category="experience",
                          status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED,
                          created_by=owner.id, department_id=dept.id,
                          source_type="workdir_sync", visibility_scope="private"))
    db.commit()

    token = _login(client, "priv_admin")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "员工私有文档" not in titles


def test_private_entry_visible_to_super_admin(client, db):
    """超管能看到所有 private 条目"""
    dept = _make_dept(db, "私有测试部门D")
    owner = _make_user(db, "priv_emp2", Role.EMPLOYEE, dept.id)
    sa = _make_user(db, "priv_sa", Role.SUPER_ADMIN, dept.id)
    entry = KnowledgeEntry(title="超管可见私有", content="x", category="experience",
                           status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED,
                           created_by=owner.id, department_id=dept.id,
                           source_type="workdir_sync", visibility_scope="private")
    db.add(entry)
    db.commit()

    token = _login(client, "priv_sa")
    resp = client.get("/api/knowledge", headers=_auth(token))
    titles = _titles(resp)
    assert "超管可见私有" in titles


def test_private_entry_detail_forbidden_for_other_employee(client, db):
    """其他员工通过详情接口也无法访问 private 条目"""
    dept = _make_dept(db, "私有测试部门E")
    owner = _make_user(db, "priv_detail_owner", Role.EMPLOYEE, dept.id)
    other = _make_user(db, "priv_detail_other", Role.EMPLOYEE, dept.id)
    entry = KnowledgeEntry(title="私有详情不可见", content="x", category="experience",
                           status=KnowledgeStatus.APPROVED, review_stage=ReviewStage.APPROVED,
                           created_by=owner.id, department_id=dept.id,
                           source_type="workdir_sync", visibility_scope="private")
    db.add(entry)
    db.commit()

    token = _login(client, "priv_detail_other")
    detail = client.get(f"/api/knowledge/{entry.id}", headers=_auth(token))
    assert detail.status_code == 403
