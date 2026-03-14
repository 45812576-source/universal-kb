import { useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/skill-policies";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Skill, Position } from "~/lib/types";

// ─── Types ────────────────────────────────────────────────────────────────────

interface SkillPolicy {
  id: number;
  skill_id: number;
  publish_scope: string;
  default_data_scope: Record<string, unknown>;
  created_at: string;
}

interface RolePolicyOverride {
  id: number;
  skill_policy_id: number;
  position_id: number;
  callable: boolean;
  data_scope: Record<string, unknown>;
  output_mask: string[];
  created_at: string;
}

interface SkillMaskOverride {
  id: number;
  skill_id: number;
  position_id: number | null;
  field_name: string;
  mask_action: string;
  mask_params: Record<string, unknown>;
  created_at: string;
}

interface AgentConnection {
  id: number;
  skill_policy_id: number;
  direction: "upstream" | "downstream";
  connected_skill_id: number;
  created_at: string;
}

// ─── Loader ───────────────────────────────────────────────────────────────────

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const [skills, policies, positions] = await Promise.all([
    apiFetch("/api/skills", { token }),
    apiFetch("/api/admin/skill-policies", { token }),
    apiFetch("/api/admin/permissions/positions", { token }),
  ]);
  return { skills, policies, positions, token };
}

// ─── Action ───────────────────────────────────────────────────────────────────

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const body = await request.json();
  const { intent, policy_id, override_id, connection_id, mask_id, ...rest } = body as {
    intent: string;
    policy_id?: number;
    override_id?: number;
    connection_id?: number;
    mask_id?: number;
    [k: string]: unknown;
  };

  if (intent === "create_policy") {
    await apiFetch("/api/admin/skill-policies", {
      method: "POST",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "update_policy") {
    await apiFetch(`/api/admin/skill-policies/${policy_id}`, {
      method: "PUT",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "upsert_override") {
    await apiFetch(`/api/admin/skill-policies/${policy_id}/overrides`, {
      method: "POST",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "delete_override") {
    await apiFetch(`/api/admin/skill-policies/${policy_id}/overrides/${override_id}`, {
      method: "DELETE",
      token,
    });
  } else if (intent === "add_connection") {
    await apiFetch(`/api/admin/skill-policies/${policy_id}/connections`, {
      method: "POST",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "delete_connection") {
    await apiFetch(`/api/admin/skill-policies/${policy_id}/connections/${connection_id}`, {
      method: "DELETE",
      token,
    });
  } else if (intent === "delete_mask") {
    await apiFetch(`/api/admin/skill-policies/${policy_id}/masks/${mask_id}`, {
      method: "DELETE",
      token,
    });
  }
  return null;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const SCOPE_LABELS: Record<string, string> = {
  self_only:   "仅自己",
  same_role:   "同角色",
  cross_role:  "跨角色",
  org_wide:    "全组织",
};

const MASK_ACTIONS = ["keep", "hide", "remove", "range", "truncate", "partial", "rank", "aggregate", "replace", "noise"];

function fmt(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// ─── Policy Detail Panel ──────────────────────────────────────────────────────

function PolicyPanel({
  policy,
  skills,
  positions,
  onClose,
}: {
  policy: SkillPolicy;
  skills: Skill[];
  positions: Position[];
  onClose: () => void;
}) {
  const fetcher = useFetcher();
  const [tab, setTab] = useState<"overrides" | "masks" | "connections">("overrides");
  const [overrides, setOverrides] = useState<RolePolicyOverride[]>([]);
  const [masks, setMasks] = useState<SkillMaskOverride[]>([]);
  const [connections, setConnections] = useState<AgentConnection[]>([]);
  const [loaded, setLoaded] = useState(false);

  // Load sub-data on mount
  if (!loaded) {
    setLoaded(true);
    Promise.all([
      fetch(`/api/admin/skill-policies/${policy.id}/overrides`, { headers: { "X-Skip-Loader": "1" } }),
      fetch(`/api/admin/skill-policies/${policy.id}/masks`, { headers: { "X-Skip-Loader": "1" } }),
      fetch(`/api/admin/skill-policies/${policy.id}/connections`, { headers: { "X-Skip-Loader": "1" } }),
    ]).then(async ([r1, r2, r3]) => {
      setOverrides(await r1.json());
      setMasks(await r2.json());
      setConnections(await r3.json());
    }).catch(() => {});
  }

  // Add override form
  const [newOverride, setNewOverride] = useState({ position_id: "", callable: true, output_mask: "" });
  // Add connection form
  const [newConn, setNewConn] = useState({ direction: "upstream", connected_skill_id: "" });
  // Edit publish scope
  const [editScope, setEditScope] = useState(policy.publish_scope);

  function submitScope() {
    fetcher.submit(
      { intent: "update_policy", policy_id: policy.id, publish_scope: editScope },
      { method: "POST", encType: "application/json" },
    );
  }

  function submitOverride() {
    if (!newOverride.position_id) return;
    fetcher.submit(
      {
        intent: "upsert_override",
        policy_id: policy.id,
        position_id: parseInt(newOverride.position_id),
        callable: newOverride.callable,
        data_scope: {},
        output_mask: newOverride.output_mask ? newOverride.output_mask.split(",").map((s) => s.trim()) : [],
      },
      { method: "POST", encType: "application/json" },
    );
    setNewOverride({ position_id: "", callable: true, output_mask: "" });
  }

  function deleteOverride(overrideId: number) {
    fetcher.submit(
      { intent: "delete_override", policy_id: policy.id, override_id: overrideId },
      { method: "POST", encType: "application/json" },
    );
    setOverrides((prev) => prev.filter((o) => o.id !== overrideId));
  }

  function submitConnection() {
    if (!newConn.connected_skill_id) return;
    fetcher.submit(
      {
        intent: "add_connection",
        policy_id: policy.id,
        direction: newConn.direction,
        connected_skill_id: parseInt(newConn.connected_skill_id),
      },
      { method: "POST", encType: "application/json" },
    );
    setNewConn({ direction: "upstream", connected_skill_id: "" });
  }

  function deleteConnection(connId: number) {
    fetcher.submit(
      { intent: "delete_connection", policy_id: policy.id, connection_id: connId },
      { method: "POST", encType: "application/json" },
    );
    setConnections((prev) => prev.filter((c) => c.id !== connId));
  }

  function deleteMask(maskId: number) {
    fetcher.submit(
      { intent: "delete_mask", policy_id: policy.id, mask_id: maskId },
      { method: "POST", encType: "application/json" },
    );
    setMasks((prev) => prev.filter((m) => m.id !== maskId));
  }

  const skill = skills.find((s) => s.id === policy.skill_id);

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white pixel-border w-full max-w-2xl flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C] flex-shrink-0">
          <span className="text-[10px] font-bold uppercase tracking-widest">
            Skill_Policy #{policy.id} — {skill?.name || `Skill ${policy.skill_id}`}
          </span>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-xs font-bold">✕</button>
        </div>

        <div className="overflow-y-auto flex-1 p-4 space-y-4">
          {/* Scope editor */}
          <div className="flex items-end gap-3">
            <div className="flex-1">
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">发布范围</div>
              <select
                value={editScope}
                onChange={(e) => setEditScope(e.target.value)}
                className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white w-full focus:outline-none focus:border-[#00A3C4]"
              >
                {Object.entries(SCOPE_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </div>
            <button
              onClick={submitScope}
              className="bg-[#1A202C] text-white px-3 py-1.5 text-[10px] font-bold uppercase hover:bg-black"
            >
              保存
            </button>
          </div>

          {/* Tabs */}
          <div className="flex gap-0 border-b-2 border-[#1A202C]">
            {(["overrides", "masks", "connections"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-4 py-2 text-[10px] font-bold uppercase tracking-wide ${
                  tab === t
                    ? "bg-[#1A202C] text-white"
                    : "text-gray-500 hover:text-[#1A202C] hover:bg-[#F0F4F8]"
                }`}
              >
                {t === "overrides" ? "角色覆盖" : t === "masks" ? "字段脱敏" : "Agent 连接"}
              </button>
            ))}
          </div>

          {/* Tab: overrides */}
          {tab === "overrides" && (
            <div className="space-y-3">
              {overrides.map((o) => {
                const pos = positions.find((p) => p.id === o.position_id);
                return (
                  <div key={o.id} className="border-2 border-[#1A202C] p-3 flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="text-xs font-bold text-[#1A202C]">{pos?.name || `岗位 #${o.position_id}`}</div>
                      <div className="flex gap-3 mt-1">
                        <span className={`text-[9px] font-bold uppercase border px-1.5 py-0.5 ${o.callable ? "bg-green-100 text-green-700 border-green-400" : "bg-red-100 text-red-600 border-red-400"}`}>
                          {o.callable ? "可调用" : "禁用"}
                        </span>
                        {o.output_mask.length > 0 && (
                          <span className="text-[9px] text-gray-500 uppercase">遮罩: {o.output_mask.join(", ")}</span>
                        )}
                      </div>
                    </div>
                    <button
                      onClick={() => deleteOverride(o.id)}
                      className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700 flex-shrink-0"
                    >
                      删除
                    </button>
                  </div>
                );
              })}
              {/* Add override */}
              <div className="border-2 border-dashed border-gray-300 p-3 space-y-2">
                <div className="text-[9px] font-bold uppercase text-gray-400">添加角色覆盖</div>
                <div className="flex gap-2">
                  <select
                    value={newOverride.position_id}
                    onChange={(e) => setNewOverride((p) => ({ ...p, position_id: e.target.value }))}
                    className="flex-1 border-2 border-[#1A202C] px-2 py-1 text-xs font-bold bg-white focus:outline-none"
                  >
                    <option value="">选择岗位</option>
                    {positions.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                  <label className="flex items-center gap-1 text-xs font-bold cursor-pointer">
                    <input
                      type="checkbox"
                      checked={newOverride.callable}
                      onChange={(e) => setNewOverride((p) => ({ ...p, callable: e.target.checked }))}
                      className="w-3 h-3"
                    />
                    可调用
                  </label>
                </div>
                <input
                  type="text"
                  value={newOverride.output_mask}
                  onChange={(e) => setNewOverride((p) => ({ ...p, output_mask: e.target.value }))}
                  placeholder="输出遮罩字段（逗号分隔，如 salary,phone）"
                  className="w-full border-2 border-[#1A202C] px-2 py-1 text-xs focus:outline-none focus:border-[#00A3C4]"
                />
                <button
                  onClick={submitOverride}
                  className="bg-[#1A202C] text-white px-3 py-1 text-[10px] font-bold uppercase hover:bg-black"
                >
                  + 添加
                </button>
              </div>
            </div>
          )}

          {/* Tab: masks */}
          {tab === "masks" && (
            <div className="space-y-2">
              {masks.length === 0 && (
                <div className="text-center py-8 text-gray-400 text-xs font-bold uppercase">暂无字段脱敏规则</div>
              )}
              {masks.map((m) => {
                const pos = positions.find((p) => p.id === m.position_id);
                return (
                  <div key={m.id} className="border-2 border-[#1A202C] p-3 flex items-center justify-between">
                    <div>
                      <span className="text-xs font-bold text-[#1A202C]">{m.field_name}</span>
                      <span className="mx-2 text-gray-300">→</span>
                      <span className="text-xs font-bold text-[#00A3C4] uppercase">{m.mask_action}</span>
                      {m.position_id && (
                        <span className="ml-2 text-[9px] text-gray-400 uppercase">[{pos?.name || `岗位 #${m.position_id}`}]</span>
                      )}
                    </div>
                    <button
                      onClick={() => deleteMask(m.id)}
                      className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700"
                    >
                      删除
                    </button>
                  </div>
                );
              })}
              <div className="text-[9px] text-gray-400 font-bold uppercase mt-2">
                通过 API <code className="bg-gray-100 px-1">POST /api/admin/skill-policies/{"{policy_id}"}/masks</code> 批量设置
              </div>
            </div>
          )}

          {/* Tab: connections */}
          {tab === "connections" && (
            <div className="space-y-3">
              {connections.map((c) => {
                const connSkill = skills.find((s) => s.id === c.connected_skill_id);
                return (
                  <div key={c.id} className="border-2 border-[#1A202C] p-3 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className={`text-[9px] font-bold uppercase border px-1.5 py-0.5 ${
                        c.direction === "upstream"
                          ? "bg-blue-100 text-blue-700 border-blue-400"
                          : "bg-purple-100 text-purple-700 border-purple-400"
                      }`}>
                        {c.direction === "upstream" ? "上游" : "下游"}
                      </span>
                      <span className="text-xs font-bold text-[#1A202C]">
                        {connSkill?.name || `Skill #${c.connected_skill_id}`}
                      </span>
                    </div>
                    <button
                      onClick={() => deleteConnection(c.id)}
                      className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700"
                    >
                      删除
                    </button>
                  </div>
                );
              })}
              {/* Add connection */}
              <div className="border-2 border-dashed border-gray-300 p-3 space-y-2">
                <div className="text-[9px] font-bold uppercase text-gray-400">添加 Agent 连接</div>
                <div className="flex gap-2">
                  <select
                    value={newConn.direction}
                    onChange={(e) => setNewConn((p) => ({ ...p, direction: e.target.value }))}
                    className="border-2 border-[#1A202C] px-2 py-1 text-xs font-bold bg-white focus:outline-none"
                  >
                    <option value="upstream">上游</option>
                    <option value="downstream">下游</option>
                  </select>
                  <select
                    value={newConn.connected_skill_id}
                    onChange={(e) => setNewConn((p) => ({ ...p, connected_skill_id: e.target.value }))}
                    className="flex-1 border-2 border-[#1A202C] px-2 py-1 text-xs font-bold bg-white focus:outline-none"
                  >
                    <option value="">选择 Skill</option>
                    {skills.filter((s) => s.id !== policy.skill_id).map((s) => (
                      <option key={s.id} value={s.id}>{s.name}</option>
                    ))}
                  </select>
                </div>
                <button
                  onClick={submitConnection}
                  className="bg-[#1A202C] text-white px-3 py-1 text-[10px] font-bold uppercase hover:bg-black"
                >
                  + 添加
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Create Policy Modal ──────────────────────────────────────────────────────

function CreatePolicyModal({
  skills,
  existingSkillIds,
  onClose,
}: {
  skills: Skill[];
  existingSkillIds: Set<number>;
  onClose: () => void;
}) {
  const fetcher = useFetcher();
  const [form, setForm] = useState({ skill_id: "", publish_scope: "same_role" });

  function submit() {
    if (!form.skill_id) return;
    fetcher.submit(
      { intent: "create_policy", skill_id: parseInt(form.skill_id), publish_scope: form.publish_scope },
      { method: "POST", encType: "application/json" },
    );
    onClose();
  }

  const available = skills.filter((s) => !existingSkillIds.has(s.id));

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white pixel-border w-full max-w-sm">
        <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
          <span className="text-[10px] font-bold uppercase tracking-widest">新建 Skill Policy</span>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-xs font-bold">✕</button>
        </div>
        <div className="p-4 space-y-3">
          <div>
            <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">选择 Skill</div>
            <select
              value={form.skill_id}
              onChange={(e) => setForm((p) => ({ ...p, skill_id: e.target.value }))}
              className="w-full border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none focus:border-[#00A3C4]"
            >
              <option value="">请选择</option>
              {available.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </div>
          <div>
            <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">发布范围</div>
            <select
              value={form.publish_scope}
              onChange={(e) => setForm((p) => ({ ...p, publish_scope: e.target.value }))}
              className="w-full border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none focus:border-[#00A3C4]"
            >
              {Object.entries(SCOPE_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </div>
          <div className="flex gap-2 pt-1">
            <button
              onClick={submit}
              className="flex-1 bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase hover:bg-black"
            >
              创建
            </button>
            <button
              onClick={onClose}
              className="flex-1 border-2 border-[#1A202C] px-4 py-2 text-[10px] font-bold uppercase hover:bg-[#F0F4F8]"
            >
              取消
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function SkillPoliciesPage() {
  const { skills, policies, positions } = useLoaderData<typeof loader>() as {
    skills: Skill[];
    policies: SkillPolicy[];
    positions: Position[];
  };

  const [selected, setSelected] = useState<SkillPolicy | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const existingSkillIds = new Set(policies.map((p) => p.skill_id));

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">Skill 权限策略</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">发布范围 / 角色覆盖 / 字段脱敏 / Agent 连接</p>
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
        >
          + 新建 Policy
        </button>
      </div>

      <div className="p-6 max-w-5xl">
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Skill_Policy_Registry</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          {policies.length === 0 ? (
            <div className="py-16 text-center text-gray-400 text-xs font-bold uppercase">暂无 Skill Policy</div>
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="border-b-2 border-[#1A202C] bg-[#F0F4F8]">
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">Skill</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">发布范围</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">创建时间</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {policies.map((p) => {
                  const skill = skills.find((s) => s.id === p.skill_id);
                  return (
                    <tr key={p.id} className="hover:bg-[#F0F4F8] transition-colors">
                      <td className="py-3 px-4">
                        <div className="text-xs font-bold text-[#1A202C]">{skill?.name || `Skill #${p.skill_id}`}</div>
                        <div className="text-[9px] text-gray-400 uppercase">policy #{p.id}</div>
                      </td>
                      <td className="py-3 px-4">
                        <span className="text-xs font-bold text-[#00A3C4] uppercase">
                          {SCOPE_LABELS[p.publish_scope] || p.publish_scope}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-[10px] text-gray-500">{fmt(p.created_at)}</td>
                      <td className="py-3 px-4 text-right">
                        <button
                          onClick={() => setSelected(p)}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          配置
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {selected && (
        <PolicyPanel
          policy={selected}
          skills={skills}
          positions={positions}
          onClose={() => setSelected(null)}
        />
      )}

      {showCreate && (
        <CreatePolicyModal
          skills={skills}
          existingSkillIds={existingSkillIds}
          onClose={() => setShowCreate(false)}
        />
      )}
    </div>
  );
}
