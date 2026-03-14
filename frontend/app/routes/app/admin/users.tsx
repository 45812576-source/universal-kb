import { useState, useCallback } from "react";
import { useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/users";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type {
  User,
  Department,
  Position,
  DataDomain,
  DataScopePolicy,
  BusinessTable,
  PolicyTargetType,
  PolicyResourceType,
  VisibilityScope,
} from "~/lib/types";

// ─── Loader ──────────────────────────────────────────────────────────────────

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const [users, departments, positions, domains, policies, tables] =
    await Promise.all([
      apiFetch("/api/admin/permissions/users", { token }),
      apiFetch("/api/admin/departments", { token }),
      apiFetch("/api/admin/permissions/positions", { token }),
      apiFetch("/api/admin/permissions/data-domains", { token }),
      apiFetch("/api/admin/permissions/policies", { token }),
      apiFetch("/api/business-tables", { token }),
    ]);
  return { users, departments, positions, domains, policies, tables, token };
}

// ─── Action ──────────────────────────────────────────────────────────────────

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const body = await request.json();
  const { intent, ...data } = body as { intent: string; [k: string]: unknown };

  if (intent === "update_user_position") {
    const { uid, position_id } = data as { uid: number; position_id: number | null };
    await apiFetch(`/api/admin/permissions/users/${uid}`, {
      method: "PUT",
      body: JSON.stringify({ position_id }),
      token,
    });
  } else if (intent === "create_position") {
    await apiFetch("/api/admin/permissions/positions", {
      method: "POST",
      body: JSON.stringify(data),
      token,
    });
  } else if (intent === "update_position") {
    const { id, ...rest } = data as { id: number; [k: string]: unknown };
    await apiFetch(`/api/admin/permissions/positions/${id}`, {
      method: "PUT",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "delete_position") {
    const { id } = data as { id: number };
    await apiFetch(`/api/admin/permissions/positions/${id}`, {
      method: "DELETE",
      token,
    });
  } else if (intent === "create_domain") {
    await apiFetch("/api/admin/permissions/data-domains", {
      method: "POST",
      body: JSON.stringify(data),
      token,
    });
  } else if (intent === "update_domain") {
    const { id, ...rest } = data as { id: number; [k: string]: unknown };
    await apiFetch(`/api/admin/permissions/data-domains/${id}`, {
      method: "PUT",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "delete_domain") {
    const { id } = data as { id: number };
    await apiFetch(`/api/admin/permissions/data-domains/${id}`, {
      method: "DELETE",
      token,
    });
  } else if (intent === "create_policy") {
    await apiFetch("/api/admin/permissions/policies", {
      method: "POST",
      body: JSON.stringify(data),
      token,
    });
  } else if (intent === "delete_policy") {
    const { id } = data as { id: number };
    await apiFetch(`/api/admin/permissions/policies/${id}`, {
      method: "DELETE",
      token,
    });
  }
  return null;
}

// ─── Shared UI atoms ─────────────────────────────────────────────────────────

function PixelInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${props.className || ""}`}
    />
  );
}

function PixelSelect(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${props.className || ""}`}
    />
  );
}

function PixelTextarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-none ${props.className || ""}`}
    />
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
      {children}
    </label>
  );
}

function PanelHeader({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
      <span className="text-[10px] font-bold uppercase tracking-widest">{title}</span>
      <div className="flex items-center gap-3">
        {action}
        <div className="flex space-x-1.5">
          <div className="w-2 h-2 bg-red-400" />
          <div className="w-2 h-2 bg-yellow-400" />
          <div className="w-2 h-2 bg-green-400" />
        </div>
      </div>
    </div>
  );
}

function Badge({ children, color = "cyan" }: { children: React.ReactNode; color?: string }) {
  const cls =
    color === "cyan"
      ? "border-[#00D1FF] bg-[#CCF2FF] text-[#00A3C4]"
      : color === "green"
        ? "border-green-400 bg-green-50 text-green-700"
        : "border-gray-300 bg-gray-100 text-gray-600";
  return (
    <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${cls}`}>
      {children}
    </span>
  );
}

// ─── Tab 1: 用户管理 ──────────────────────────────────────────────────────────

function UsersTab({
  users,
  departments,
  positions,
}: {
  users: User[];
  departments: Department[];
  positions: Position[];
}) {
  const fetcher = useFetcher();
  const [selectedDept, setSelectedDept] = useState<number | null>(null);
  const [editingUserId, setEditingUserId] = useState<number | null>(null);
  const [pendingPositionId, setPendingPositionId] = useState<number | null>(null);

  const deptTree = buildDeptTree(departments);
  const filtered = selectedDept
    ? users.filter((u) => u.department_id === selectedDept)
    : users;

  const startEdit = (u: User) => {
    setEditingUserId(u.id);
    setPendingPositionId(u.position_id ?? null);
  };

  const saveEdit = (u: User) => {
    fetcher.submit(
      JSON.stringify({ intent: "update_user_position", uid: u.id, position_id: pendingPositionId }),
      { method: "post", encType: "application/json" },
    );
    setEditingUserId(null);
  };

  const deptPositions = positions.filter(
    (p) => !selectedDept || p.department_id === selectedDept,
  );

  return (
    <div className="flex gap-6">
      {/* 左：部门树 */}
      <div className="w-56 shrink-0">
        <div className="pixel-border bg-white overflow-hidden">
          <PanelHeader title="部门" />
          <div className="divide-y divide-gray-100">
            <button
              onClick={() => setSelectedDept(null)}
              className={`w-full text-left px-4 py-2.5 text-xs font-bold transition-colors ${!selectedDept ? "bg-[#00D1FF]/10 text-[#00A3C4]" : "hover:bg-gray-50 text-[#1A202C]"}`}
            >
              全部
            </button>
            {renderDeptNodes(deptTree, selectedDept, setSelectedDept)}
          </div>
        </div>
      </div>

      {/* 右：用户表 */}
      <div className="flex-1 min-w-0">
        <div className="pixel-border bg-white overflow-hidden">
          <PanelHeader title={`用户列表 (${filtered.length})`} />
          <table className="w-full text-left">
            <thead>
              <tr className="border-b-2 border-[#1A202C] bg-[#F0F4F8]">
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">姓名</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">账号</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">角色</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">部门</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">岗位</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {filtered.map((u) => (
                <tr key={u.id} className="hover:bg-[#F0F4F8] transition-colors">
                  <td className="py-3 px-4 text-xs font-bold text-[#1A202C]">{u.display_name}</td>
                  <td className="py-3 px-4 text-[10px] font-mono text-gray-500">{u.username}</td>
                  <td className="py-3 px-4">
                    <RoleBadge role={u.role} />
                  </td>
                  <td className="py-3 px-4 text-[10px] text-gray-500">{u.department_name || "—"}</td>
                  <td className="py-3 px-4">
                    {editingUserId === u.id ? (
                      <PixelSelect
                        value={pendingPositionId ?? ""}
                        onChange={(e) =>
                          setPendingPositionId(e.target.value ? Number(e.target.value) : null)
                        }
                        className="w-36"
                      >
                        <option value="">无岗位</option>
                        {deptPositions.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </PixelSelect>
                    ) : (
                      <span className="text-[10px] text-gray-500">
                        {u.position_name || "—"}
                      </span>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center justify-end gap-3">
                      {editingUserId === u.id ? (
                        <>
                          <button
                            onClick={() => saveEdit(u)}
                            className="text-[10px] font-bold uppercase text-green-600 hover:underline"
                          >
                            保存
                          </button>
                          <button
                            onClick={() => setEditingUserId(null)}
                            className="text-[10px] font-bold uppercase text-gray-400 hover:underline"
                          >
                            取消
                          </button>
                        </>
                      ) : (
                        <button
                          onClick={() => startEdit(u)}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          分配岗位
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-xs font-bold uppercase text-gray-400">
                    暂无用户
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ─── Tab 2: 岗位管理 ──────────────────────────────────────────────────────────

function PositionsTab({
  positions,
  departments,
}: {
  positions: Position[];
  departments: Department[];
}) {
  const fetcher = useFetcher();
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState({ name: "", department_id: "", description: "" });

  const submit = (intent: string, data: object) => {
    fetcher.submit(JSON.stringify({ intent, ...data }), {
      method: "post",
      encType: "application/json",
    });
  };

  const openNew = () => {
    setForm({ name: "", department_id: "", description: "" });
    setCreating(true);
    setEditingId(null);
  };

  const openEdit = (p: Position) => {
    setForm({
      name: p.name,
      department_id: String(p.department_id ?? ""),
      description: p.description ?? "",
    });
    setEditingId(p.id);
    setCreating(false);
  };

  const handleSave = () => {
    const payload = {
      name: form.name,
      department_id: form.department_id ? Number(form.department_id) : null,
      description: form.description || null,
    };
    if (creating) {
      submit("create_position", payload);
    } else if (editingId) {
      submit("update_position", { id: editingId, ...payload });
    }
    setCreating(false);
    setEditingId(null);
  };

  const handleDelete = (id: number) => {
    if (!confirm("确定删除此岗位？")) return;
    submit("delete_position", { id });
  };

  // Group by department
  const grouped: Record<string, Position[]> = {};
  for (const p of positions) {
    const key = p.department_name || "未分配部门";
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(p);
  }

  return (
    <div>
      <div className="flex justify-end mb-4">
        <button
          onClick={openNew}
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
        >
          + 新增岗位
        </button>
      </div>

      {/* 新建/编辑表单 */}
      {(creating || editingId !== null) && (
        <div className="pixel-border bg-white mb-4">
          <PanelHeader title={creating ? "新增岗位" : "编辑岗位"} />
          <div className="p-5 grid grid-cols-3 gap-4">
            <div>
              <FieldLabel>岗位名称 *</FieldLabel>
              <PixelInput
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：高级销售"
              />
            </div>
            <div>
              <FieldLabel>所属部门</FieldLabel>
              <PixelSelect
                value={form.department_id}
                onChange={(e) => setForm({ ...form, department_id: e.target.value })}
              >
                <option value="">不指定</option>
                {departments.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </PixelSelect>
            </div>
            <div>
              <FieldLabel>描述</FieldLabel>
              <PixelInput
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="简要描述"
              />
            </div>
          </div>
          <div className="px-5 pb-4 flex gap-3">
            <button
              onClick={handleSave}
              className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
            >
              保存
            </button>
            <button
              onClick={() => { setCreating(false); setEditingId(null); }}
              className="border-2 border-[#1A202C] bg-white px-5 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {/* 按部门分组 */}
      <div className="space-y-4">
        {Object.entries(grouped).map(([deptName, pos]) => (
          <div key={deptName} className="pixel-border bg-white overflow-hidden">
            <PanelHeader title={deptName} />
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-gray-200 bg-[#F0F4F8]">
                  <th className="py-2.5 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">岗位名称</th>
                  <th className="py-2.5 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">描述</th>
                  <th className="py-2.5 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {pos.map((p) => (
                  <tr key={p.id} className="hover:bg-[#F0F4F8] transition-colors">
                    <td className="py-3 px-4 text-xs font-bold text-[#1A202C]">{p.name}</td>
                    <td className="py-3 px-4 text-[10px] text-gray-500">{p.description || "—"}</td>
                    <td className="py-3 px-4">
                      <div className="flex items-center justify-end gap-3">
                        <button
                          onClick={() => openEdit(p)}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          编辑
                        </button>
                        <button
                          onClick={() => handleDelete(p.id)}
                          className="text-[10px] font-bold uppercase text-red-500 hover:underline"
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        {positions.length === 0 && !creating && (
          <div className="pixel-border bg-white p-8 text-center">
            <p className="text-xs font-bold uppercase text-gray-400">暂无岗位 — 点击右上角新增</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Tab 3: 数据权限配置 ──────────────────────────────────────────────────────

const ROLES = [
  { value: "super_admin", label: "Super Admin" },
  { value: "dept_admin", label: "Dept Admin" },
  { value: "employee", label: "Employee" },
];

const VISIBILITY_OPTIONS: { value: VisibilityScope; label: string }[] = [
  { value: "own", label: "仅本人" },
  { value: "dept", label: "本部门" },
  { value: "all", label: "全部" },
];

function PolicyEditor({
  tables,
  domains,
  onSave,
  onCancel,
  targetType,
  targetPositionId,
  targetRole,
  authToken,
}: {
  tables: BusinessTable[];
  domains: DataDomain[];
  onSave: (data: object) => void;
  onCancel: () => void;
  targetType: PolicyTargetType;
  targetPositionId: number | null;
  targetRole: string | null;
  authToken: string;
}) {
  const [resourceType, setResourceType] = useState<PolicyResourceType>("business_table");
  const [tableId, setTableId] = useState<number | null>(null);
  const [domainId, setDomainId] = useState<number | null>(null);
  const [visibility, setVisibility] = useState<VisibilityScope>("own");
  const [outputMask, setOutputMask] = useState<string[]>([]);
  const [tableColumns, setTableColumns] = useState<{ name: string; comment: string }[]>([]);

  const loadColumns = async (tid: number) => {
    try {
      const data = await apiFetch(`/api/admin/permissions/business-table-columns/${tid}`, {
        token: authToken,
      });
      setTableColumns(data.columns || []);
    } catch {
      setTableColumns([]);
    }
  };

  const handleTableChange = (tid: number | null) => {
    setTableId(tid);
    setOutputMask([]);
    if (tid) loadColumns(tid);
    else setTableColumns([]);
  };

  const toggleMask = (name: string) => {
    setOutputMask((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name],
    );
  };

  const selectedDomain = domains.find((d) => d.id === domainId);

  const handleSave = () => {
    onSave({
      target_type: targetType,
      target_position_id: targetPositionId,
      target_role: targetRole,
      resource_type: resourceType,
      business_table_id: resourceType === "business_table" ? tableId : null,
      data_domain_id: resourceType === "data_domain" ? domainId : null,
      visibility_level: visibility,
      output_mask: outputMask,
    });
  };

  const maskFields =
    resourceType === "business_table"
      ? tableColumns.map((c) => ({ name: c.name, label: c.comment || c.name }))
      : (selectedDomain?.fields || []).map((f) => ({ name: f.name, label: f.label }));

  return (
    <div className="pixel-border bg-white mb-4">
      <PanelHeader title="新增策略" />
      <div className="p-5 space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <FieldLabel>资源类型</FieldLabel>
            <PixelSelect
              value={resourceType}
              onChange={(e) => {
                setResourceType(e.target.value as PolicyResourceType);
                setOutputMask([]);
                setTableId(null);
                setDomainId(null);
                setTableColumns([]);
              }}
            >
              <option value="business_table">业务表</option>
              <option value="data_domain">数据域</option>
            </PixelSelect>
          </div>
          <div>
            <FieldLabel>可见范围</FieldLabel>
            <PixelSelect
              value={visibility}
              onChange={(e) => setVisibility(e.target.value as VisibilityScope)}
            >
              {VISIBILITY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </PixelSelect>
          </div>
        </div>

        {resourceType === "business_table" && (
          <div>
            <FieldLabel>选择业务表</FieldLabel>
            <PixelSelect
              value={tableId ?? ""}
              onChange={(e) => handleTableChange(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">— 请选择 —</option>
              {tables.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.display_name}
                </option>
              ))}
            </PixelSelect>
          </div>
        )}

        {resourceType === "data_domain" && (
          <div>
            <FieldLabel>选择数据域</FieldLabel>
            <PixelSelect
              value={domainId ?? ""}
              onChange={(e) => {
                setDomainId(e.target.value ? Number(e.target.value) : null);
                setOutputMask([]);
              }}
            >
              <option value="">— 请选择 —</option>
              {domains.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.display_name}
                </option>
              ))}
            </PixelSelect>
          </div>
        )}

        {maskFields.length > 0 && (
          <div>
            <FieldLabel>脱敏字段（勾选后对该目标隐藏/脱敏）</FieldLabel>
            <div className="border-2 border-[#1A202C] p-3 grid grid-cols-3 gap-2">
              {maskFields.map((f) => (
                <label
                  key={f.name}
                  className="flex items-center gap-2 cursor-pointer text-xs font-bold text-[#1A202C]"
                >
                  <input
                    type="checkbox"
                    checked={outputMask.includes(f.name)}
                    onChange={() => toggleMask(f.name)}
                    className="border-2 border-[#1A202C] w-3.5 h-3.5"
                  />
                  <span className="font-mono">{f.name}</span>
                  {f.label !== f.name && (
                    <span className="text-gray-400 font-normal">({f.label})</span>
                  )}
                </label>
              ))}
            </div>
          </div>
        )}

        <div className="flex gap-3 pt-1">
          <button
            onClick={handleSave}
            className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
          >
            保存策略
          </button>
          <button
            onClick={onCancel}
            className="border-2 border-[#1A202C] bg-white px-5 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
          >
            取消
          </button>
        </div>
      </div>
    </div>
  );
}

function PolicyCard({
  policy,
  onDelete,
}: {
  policy: DataScopePolicy;
  onDelete: (id: number) => void;
}) {
  const resourceLabel =
    policy.resource_type === "business_table"
      ? policy.business_table_name || `表#${policy.business_table_id}`
      : policy.data_domain_name || `域#${policy.data_domain_id}`;

  const visLabel = { own: "仅本人", dept: "本部门", all: "全部" }[policy.visibility_level] || policy.visibility_level;

  return (
    <div className="border-2 border-[#1A202C] p-4 flex items-start justify-between gap-4">
      <div className="space-y-2 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge color="cyan">
            {policy.resource_type === "business_table" ? "业务表" : "数据域"}
          </Badge>
          <span className="text-xs font-bold text-[#1A202C]">{resourceLabel}</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] font-bold uppercase text-gray-500">可见范围：</span>
          <Badge color="green">{visLabel}</Badge>
          {policy.output_mask.length > 0 && (
            <>
              <span className="text-[10px] font-bold uppercase text-gray-500">脱敏字段：</span>
              {policy.output_mask.map((f) => (
                <span
                  key={f}
                  className="font-mono text-[9px] bg-red-50 border border-red-200 text-red-600 px-1.5 py-0.5"
                >
                  {f}
                </span>
              ))}
            </>
          )}
        </div>
      </div>
      <button
        onClick={() => onDelete(policy.id)}
        className="shrink-0 text-[10px] font-bold uppercase text-red-500 hover:underline"
      >
        删除
      </button>
    </div>
  );
}

function PermissionsTab({
  policies,
  positions,
  departments,
  tables,
  domains,
  authToken,
}: {
  policies: DataScopePolicy[];
  positions: Position[];
  departments: Department[];
  tables: BusinessTable[];
  domains: DataDomain[];
  authToken: string;
}) {
  const fetcher = useFetcher();
  const [targetMode, setTargetMode] = useState<PolicyTargetType>("position");
  const [selectedPositionId, setSelectedPositionId] = useState<number | null>(null);
  const [selectedRole, setSelectedRole] = useState<string>("employee");
  const [addingPolicy, setAddingPolicy] = useState(false);
  const [selectedDept, setSelectedDept] = useState<number | null>(null);

  const submit = (intent: string, data: object) => {
    fetcher.submit(JSON.stringify({ intent, ...data }), {
      method: "post",
      encType: "application/json",
    });
  };

  const filteredPolicies = policies.filter((p) => {
    if (targetMode === "position") {
      return p.target_type === "position" && p.target_position_id === selectedPositionId;
    } else {
      return p.target_type === "role" && p.target_role === selectedRole;
    }
  });

  const deptPositions = positions.filter(
    (p) => !selectedDept || p.department_id === selectedDept,
  );

  return (
    <div className="flex gap-6">
      {/* 左：Target 选择器 */}
      <div className="w-64 shrink-0 space-y-4">
        <div className="pixel-border bg-white overflow-hidden">
          <PanelHeader title="目标类型" />
          <div className="p-4 space-y-3">
            <div className="flex gap-2">
              <button
                onClick={() => { setTargetMode("position"); setAddingPolicy(false); }}
                className={`flex-1 py-2 text-[10px] font-bold uppercase border-2 transition-colors ${targetMode === "position" ? "bg-[#1A202C] text-white border-[#1A202C]" : "bg-white text-[#1A202C] border-[#1A202C] hover:bg-gray-50"}`}
              >
                岗位
              </button>
              <button
                onClick={() => { setTargetMode("role"); setAddingPolicy(false); }}
                className={`flex-1 py-2 text-[10px] font-bold uppercase border-2 transition-colors ${targetMode === "role" ? "bg-[#1A202C] text-white border-[#1A202C]" : "bg-white text-[#1A202C] border-[#1A202C] hover:bg-gray-50"}`}
              >
                角色
              </button>
            </div>

            {targetMode === "position" && (
              <div className="space-y-2">
                <div>
                  <FieldLabel>部门筛选</FieldLabel>
                  <PixelSelect
                    value={selectedDept ?? ""}
                    onChange={(e) => {
                      setSelectedDept(e.target.value ? Number(e.target.value) : null);
                      setSelectedPositionId(null);
                      setAddingPolicy(false);
                    }}
                  >
                    <option value="">全部部门</option>
                    {departments.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name}
                      </option>
                    ))}
                  </PixelSelect>
                </div>
                <div>
                  <FieldLabel>选择岗位</FieldLabel>
                  <PixelSelect
                    value={selectedPositionId ?? ""}
                    onChange={(e) => {
                      setSelectedPositionId(e.target.value ? Number(e.target.value) : null);
                      setAddingPolicy(false);
                    }}
                  >
                    <option value="">— 请选择 —</option>
                    {deptPositions.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </PixelSelect>
                </div>
              </div>
            )}

            {targetMode === "role" && (
              <div>
                <FieldLabel>选择角色</FieldLabel>
                <div className="space-y-1">
                  {ROLES.map((r) => (
                    <button
                      key={r.value}
                      onClick={() => { setSelectedRole(r.value); setAddingPolicy(false); }}
                      className={`w-full text-left px-3 py-2 text-xs font-bold border-2 transition-colors ${selectedRole === r.value ? "bg-[#1A202C] text-white border-[#1A202C]" : "bg-white text-[#1A202C] border-gray-200 hover:border-[#1A202C]"}`}
                    >
                      {r.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 右：策略列表 */}
      <div className="flex-1 min-w-0">
        {(targetMode === "role" || selectedPositionId) ? (
          <>
            <div className="flex items-center justify-between mb-4">
              <div>
                <p className="text-[10px] font-bold uppercase text-gray-500">
                  当前目标：
                  <span className="text-[#1A202C] ml-1">
                    {targetMode === "role"
                      ? ROLES.find((r) => r.value === selectedRole)?.label
                      : positions.find((p) => p.id === selectedPositionId)?.name}
                  </span>
                </p>
                <p className="text-[9px] text-gray-400 mt-0.5">
                  {filteredPolicies.length} 条策略
                </p>
              </div>
              <button
                onClick={() => setAddingPolicy(true)}
                className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
              >
                + 新增策略
              </button>
            </div>

            {addingPolicy && (
              <PolicyEditor
                tables={tables}
                domains={domains}
                targetType={targetMode}
                targetPositionId={targetMode === "position" ? selectedPositionId : null}
                targetRole={targetMode === "role" ? selectedRole : null}
                authToken={authToken}
                onSave={(data) => {
                  submit("create_policy", data);
                  setAddingPolicy(false);
                }}
                onCancel={() => setAddingPolicy(false)}
              />
            )}

            <div className="pixel-border bg-white overflow-hidden">
              <PanelHeader title="策略列表" />
              <div className="p-4 space-y-3">
                {filteredPolicies.length === 0 ? (
                  <p className="py-8 text-center text-xs font-bold uppercase text-gray-400">
                    暂无策略 — 点击右上角新增
                  </p>
                ) : (
                  filteredPolicies.map((p) => (
                    <PolicyCard
                      key={p.id}
                      policy={p}
                      onDelete={(id) => {
                        if (!confirm("确定删除此策略？")) return;
                        submit("delete_policy", { id });
                      }}
                    />
                  ))
                )}
              </div>
            </div>
          </>
        ) : (
          <div className="pixel-border bg-white p-12 text-center">
            <p className="text-xs font-bold uppercase text-gray-400">
              ← 请先在左侧选择目标岗位或角色
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Dept tree helpers ────────────────────────────────────────────────────────

type DeptNode = Department & { children: DeptNode[] };

function buildDeptTree(departments: Department[]): DeptNode[] {
  const map = new Map<number, DeptNode>();
  for (const d of departments) map.set(d.id, { ...d, children: [] });
  const roots: DeptNode[] = [];
  for (const node of map.values()) {
    if (node.parent_id && map.has(node.parent_id)) {
      map.get(node.parent_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

function renderDeptNodes(
  nodes: DeptNode[],
  selectedDept: number | null,
  setSelected: (id: number | null) => void,
  depth = 0,
): React.ReactNode {
  return nodes.map((n) => (
    <div key={n.id}>
      <button
        onClick={() => setSelected(n.id)}
        className={`w-full text-left px-4 py-2.5 text-xs font-bold transition-colors ${selectedDept === n.id ? "bg-[#00D1FF]/10 text-[#00A3C4]" : "hover:bg-gray-50 text-[#1A202C]"}`}
        style={{ paddingLeft: `${16 + depth * 16}px` }}
      >
        {depth > 0 && <span className="text-gray-300 mr-1">└</span>}
        {n.name}
      </button>
      {n.children.length > 0 && renderDeptNodes(n.children, selectedDept, setSelected, depth + 1)}
    </div>
  ));
}

function RoleBadge({ role }: { role: string }) {
  const map: Record<string, string> = {
    super_admin: "超管",
    dept_admin: "部门管理员",
    employee: "员工",
  };
  const color =
    role === "super_admin" ? "cyan" : role === "dept_admin" ? "green" : "default";
  return <Badge color={color}>{map[role] || role}</Badge>;
}

// ─── Page ─────────────────────────────────────────────────────────────────────

type LoaderData = {
  users: User[];
  departments: Department[];
  positions: Position[];
  domains: DataDomain[];
  policies: DataScopePolicy[];
  tables: BusinessTable[];
  token: string;
};

export default function UsersPage() {
  const { users, departments, positions, domains, policies, tables, token } =
    useLoaderData<typeof loader>() as LoaderData;
  const [tab, setTab] = useState<0 | 1 | 2>(0);

  const TABS = ["用户管理", "岗位管理", "数据权限"];

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">
          用户与权限管理
        </h1>
      </div>

      {/* Tabs */}
      <div className="border-b-2 border-[#1A202C] bg-white px-6 flex gap-0">
        {TABS.map((label, i) => (
          <button
            key={i}
            onClick={() => setTab(i as 0 | 1 | 2)}
            className={`px-5 py-3 text-[10px] font-bold uppercase tracking-widest border-r-2 border-[#1A202C] transition-colors ${tab === i ? "bg-[#1A202C] text-white" : "bg-white text-[#1A202C] hover:bg-[#F0F4F8]"}`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="p-6">
        {tab === 0 && (
          <UsersTab users={users} departments={departments} positions={positions} />
        )}
        {tab === 1 && (
          <PositionsTab positions={positions} departments={departments} />
        )}
        {tab === 2 && (
          <PermissionsTab
            policies={policies}
            positions={positions}
            departments={departments}
            tables={tables}
            domains={domains}
            authToken={token}
          />
        )}
      </div>
    </div>
  );
}
