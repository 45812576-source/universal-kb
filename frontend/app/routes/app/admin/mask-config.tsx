import { useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/mask-config";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Position, DataDomain } from "~/lib/types";

// ─── Types ────────────────────────────────────────────────────────────────────

interface GlobalMask {
  id: number;
  field_name: string;
  data_domain_id: number | null;
  mask_action: string;
  mask_params: Record<string, unknown>;
  severity: string | null;
  created_at: string;
}

interface RoleMask {
  id: number;
  position_id: number;
  field_name: string;
  data_domain_id: number | null;
  mask_action: string;
  mask_params: Record<string, unknown>;
  created_at: string;
}

interface OutputMask {
  id: number;
  position_id: number;
  data_domain_id: number;
  field_name: string;
  mask_action: string;
  created_at: string;
}

// ─── Loader ───────────────────────────────────────────────────────────────────

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const [positions, domains, globalMasks, roleMasks, outputMasks] = await Promise.all([
    apiFetch("/api/admin/permissions/positions", { token }),
    apiFetch("/api/admin/permissions/data-domains", { token }),
    apiFetch("/api/admin/permissions/global-masks", { token }),
    apiFetch("/api/admin/permissions/role-masks", { token }),
    apiFetch("/api/admin/permissions/output-masks", { token }),
  ]);
  return { positions, domains, globalMasks, roleMasks, outputMasks, token };
}

// ─── Action ───────────────────────────────────────────────────────────────────

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const body = await request.json();
  const { intent, id, ...rest } = body as { intent: string; id?: number; [k: string]: unknown };

  if (intent === "create_global_mask") {
    await apiFetch("/api/admin/permissions/global-masks", {
      method: "POST",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "delete_global_mask") {
    await apiFetch(`/api/admin/permissions/global-masks/${id}`, { method: "DELETE", token });
  } else if (intent === "create_role_mask") {
    await apiFetch("/api/admin/permissions/role-masks", {
      method: "POST",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "delete_role_mask") {
    await apiFetch(`/api/admin/permissions/role-masks/${id}`, { method: "DELETE", token });
  } else if (intent === "create_output_mask") {
    await apiFetch("/api/admin/permissions/output-masks", {
      method: "POST",
      body: JSON.stringify(rest),
      token,
    });
  } else if (intent === "delete_output_mask") {
    await apiFetch(`/api/admin/permissions/output-masks/${id}`, { method: "DELETE", token });
  }
  return null;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const MASK_ACTIONS = ["keep", "hide", "remove", "range", "truncate", "partial", "rank", "aggregate", "replace", "noise"];

const MASK_ACTION_COLORS: Record<string, string> = {
  keep:      "bg-green-100 text-green-700 border-green-400",
  noise:     "bg-green-100 text-green-700 border-green-400",
  range:     "bg-yellow-100 text-yellow-700 border-yellow-400",
  rank:      "bg-yellow-100 text-yellow-700 border-yellow-400",
  truncate:  "bg-orange-100 text-orange-700 border-orange-400",
  partial:   "bg-orange-100 text-orange-700 border-orange-400",
  aggregate: "bg-red-100 text-red-700 border-red-400",
  replace:   "bg-red-100 text-red-700 border-red-400",
  remove:    "bg-gray-200 text-gray-700 border-gray-500",
  hide:      "bg-gray-200 text-gray-700 border-gray-500",
};

// ─── Preview Modal ────────────────────────────────────────────────────────────

function PreviewModal({
  positions,
  domains,
  token,
  onClose,
}: {
  positions: Position[];
  domains: DataDomain[];
  token: string;
  onClose: () => void;
}) {
  const [posId, setPosId] = useState("");
  const [domainId, setDomainId] = useState("");
  const [sampleText, setSampleText] = useState('[\n  {"name": "张三", "phone": "13800001234", "salary": 18000}\n]');
  const [result, setResult] = useState<{ masked: unknown[]; original_count: number } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function runPreview() {
    if (!posId) return;
    setLoading(true);
    setError("");
    try {
      const sample = JSON.parse(sampleText);
      const res = await fetch("/api/admin/permissions/mask-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          position_id: parseInt(posId),
          data_domain_id: domainId ? parseInt(domainId) : null,
          sample_data: sample,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      setResult(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white pixel-border w-full max-w-2xl flex flex-col max-h-[90vh]">
        <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C] flex-shrink-0">
          <span className="text-[10px] font-bold uppercase tracking-widest">脱敏效果预览</span>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-xs font-bold">✕</button>
        </div>
        <div className="overflow-y-auto flex-1 p-4 space-y-3">
          <div className="flex gap-3">
            <div className="flex-1">
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">岗位</div>
              <select
                value={posId}
                onChange={(e) => setPosId(e.target.value)}
                className="w-full border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none focus:border-[#00A3C4]"
              >
                <option value="">请选择</option>
                {positions.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
            <div className="flex-1">
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">数据域（可选）</div>
              <select
                value={domainId}
                onChange={(e) => setDomainId(e.target.value)}
                className="w-full border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none focus:border-[#00A3C4]"
              >
                <option value="">全部</option>
                {domains.map((d) => <option key={d.id} value={d.id}>{d.display_name}</option>)}
              </select>
            </div>
          </div>
          <div>
            <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">示例数据（JSON 数组）</div>
            <textarea
              value={sampleText}
              onChange={(e) => setSampleText(e.target.value)}
              rows={5}
              className="w-full text-xs border-2 border-[#1A202C] px-2 py-1.5 font-mono resize-none focus:outline-none focus:border-[#00A3C4]"
            />
          </div>
          <button
            onClick={runPreview}
            disabled={!posId || loading}
            className="bg-[#1A202C] text-white px-4 py-1.5 text-[10px] font-bold uppercase tracking-wide hover:bg-black disabled:opacity-50"
          >
            {loading ? "处理中..." : "运行预览"}
          </button>
          {error && (
            <div className="border-2 border-red-400 bg-red-50 p-3 text-xs text-red-700 font-mono">{error}</div>
          )}
          {result && (
            <div className="space-y-2">
              <div className="text-[9px] font-bold uppercase text-gray-400">脱敏结果（{result.original_count} 条）</div>
              <pre className="bg-[#F0F4F8] border-2 border-[#1A202C] p-3 text-xs font-mono overflow-x-auto">
                {JSON.stringify(result.masked, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Add Mask Row Form ────────────────────────────────────────────────────────

function AddMaskRow({
  label,
  fields,
  onSubmit,
}: {
  label: string;
  fields: React.ReactNode;
  onSubmit: () => void;
}) {
  return (
    <div className="border-t-2 border-dashed border-gray-200 pt-3 mt-2">
      <div className="text-[9px] font-bold uppercase text-gray-400 mb-2">{label}</div>
      <div className="flex flex-wrap gap-2 items-end">
        {fields}
        <button
          onClick={onSubmit}
          className="bg-[#1A202C] text-white px-3 py-1.5 text-[10px] font-bold uppercase hover:bg-black"
        >
          + 添加
        </button>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function MaskConfigPage() {
  const { positions, domains, globalMasks, roleMasks, outputMasks, token } =
    useLoaderData<typeof loader>() as {
      positions: Position[];
      domains: DataDomain[];
      globalMasks: GlobalMask[];
      roleMasks: RoleMask[];
      outputMasks: OutputMask[];
      token: string;
    };

  const fetcher = useFetcher();
  const [tab, setTab] = useState<"global" | "role" | "output">("global");
  const [showPreview, setShowPreview] = useState(false);

  // Filter by position (for role/output tabs)
  const [filterPosId, setFilterPosId] = useState("");
  const [filterDomainId, setFilterDomainId] = useState("");

  // New global mask form
  const [newGlobal, setNewGlobal] = useState({ field_name: "", data_domain_id: "", mask_action: "hide" });
  // New role mask form
  const [newRole, setNewRole] = useState({ position_id: "", field_name: "", data_domain_id: "", mask_action: "hide" });
  // New output mask form
  const [newOutput, setNewOutput] = useState({ position_id: "", data_domain_id: "", field_name: "", mask_action: "show" });

  const filteredRoleMasks = roleMasks.filter((m) => {
    if (filterPosId && m.position_id !== parseInt(filterPosId)) return false;
    if (filterDomainId && m.data_domain_id !== parseInt(filterDomainId)) return false;
    return true;
  });

  const filteredOutputMasks = outputMasks.filter((m) => {
    if (filterPosId && m.position_id !== parseInt(filterPosId)) return false;
    if (filterDomainId && m.data_domain_id !== parseInt(filterDomainId)) return false;
    return true;
  });

  function posName(id: number | null) {
    if (!id) return "—";
    return positions.find((p) => p.id === id)?.name || `#${id}`;
  }

  function domainName(id: number | null) {
    if (!id) return "全局";
    return domains.find((d) => d.id === id)?.display_name || `#${id}`;
  }

  function del(intent: string, id: number) {
    fetcher.submit({ intent, id }, { method: "POST", encType: "application/json" });
  }

  function addGlobal() {
    if (!newGlobal.field_name) return;
    fetcher.submit(
      {
        intent: "create_global_mask",
        field_name: newGlobal.field_name,
        data_domain_id: newGlobal.data_domain_id ? parseInt(newGlobal.data_domain_id) : null,
        mask_action: newGlobal.mask_action,
      },
      { method: "POST", encType: "application/json" },
    );
    setNewGlobal({ field_name: "", data_domain_id: "", mask_action: "hide" });
  }

  function addRole() {
    if (!newRole.position_id || !newRole.field_name) return;
    fetcher.submit(
      {
        intent: "create_role_mask",
        position_id: parseInt(newRole.position_id),
        field_name: newRole.field_name,
        data_domain_id: newRole.data_domain_id ? parseInt(newRole.data_domain_id) : null,
        mask_action: newRole.mask_action,
      },
      { method: "POST", encType: "application/json" },
    );
    setNewRole({ position_id: "", field_name: "", data_domain_id: "", mask_action: "hide" });
  }

  function addOutput() {
    if (!newOutput.position_id || !newOutput.data_domain_id || !newOutput.field_name) return;
    fetcher.submit(
      {
        intent: "create_output_mask",
        position_id: parseInt(newOutput.position_id),
        data_domain_id: parseInt(newOutput.data_domain_id),
        field_name: newOutput.field_name,
        mask_action: newOutput.mask_action,
      },
      { method: "POST", encType: "application/json" },
    );
    setNewOutput({ position_id: "", data_domain_id: "", field_name: "", mask_action: "show" });
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">脱敏规则配置</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">三层优先级：全局 → 角色覆盖 → Skill 级</p>
          </div>
        </div>
        <button
          onClick={() => setShowPreview(true)}
          className="border-2 border-[#1A202C] bg-white text-[#1A202C] px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-[#CCF2FF]"
        >
          脱敏预览
        </button>
      </div>

      <div className="p-6 max-w-5xl space-y-4">
        {/* Priority hint */}
        <div className="flex items-center gap-3 text-[10px] font-bold uppercase">
          <span className="bg-green-100 border border-green-400 text-green-700 px-2 py-0.5">全局默认</span>
          <span className="text-gray-400">→ 覆盖 →</span>
          <span className="bg-yellow-100 border border-yellow-400 text-yellow-700 px-2 py-0.5">角色级</span>
          <span className="text-gray-400">→ 覆盖 →</span>
          <span className="bg-red-100 border border-red-400 text-red-700 px-2 py-0.5">Skill 级（最严）</span>
        </div>

        {/* Tabs */}
        <div className="flex gap-0 border-b-2 border-[#1A202C]">
          {(["global", "role", "output"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-5 py-2 text-[10px] font-bold uppercase tracking-wide ${
                tab === t
                  ? "bg-[#1A202C] text-white"
                  : "text-gray-500 hover:text-[#1A202C] hover:bg-[#F0F4F8]"
              }`}
            >
              {t === "global" ? `全局默认 (${globalMasks.length})` : t === "role" ? `角色覆盖 (${roleMasks.length})` : `输出遮罩 (${outputMasks.length})`}
            </button>
          ))}
        </div>

        {/* Filter row for role/output */}
        {tab !== "global" && (
          <div className="flex gap-3">
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">岗位筛选</div>
              <select
                value={filterPosId}
                onChange={(e) => setFilterPosId(e.target.value)}
                className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
              >
                <option value="">全部岗位</option>
                {positions.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">数据域筛选</div>
              <select
                value={filterDomainId}
                onChange={(e) => setFilterDomainId(e.target.value)}
                className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
              >
                <option value="">全部数据域</option>
                {domains.map((d) => <option key={d.id} value={d.id}>{d.display_name}</option>)}
              </select>
            </div>
          </div>
        )}

        {/* Table */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              {tab === "global" ? "Global_Mask_Rules" : tab === "role" ? "Role_Mask_Overrides" : "Output_Masks"}
            </span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>

          <div className="divide-y divide-gray-100">
            {/* Global masks */}
            {tab === "global" && (
              <>
                {globalMasks.length === 0 && (
                  <div className="py-8 text-center text-gray-400 text-xs font-bold uppercase">暂无全局脱敏规则</div>
                )}
                {globalMasks.map((m) => (
                  <div key={m.id} className="px-4 py-3 flex items-center justify-between hover:bg-[#F0F4F8]">
                    <div className="flex items-center gap-3 flex-1 min-w-0">
                      <span className="text-xs font-bold text-[#1A202C] w-36 truncate">{m.field_name}</span>
                      <span className="text-[9px] text-gray-400 uppercase w-24 truncate">{domainName(m.data_domain_id)}</span>
                      <span className={`border px-2 py-0.5 text-[9px] font-bold uppercase ${MASK_ACTION_COLORS[m.mask_action] || "bg-gray-100 text-gray-600 border-gray-400"}`}>
                        {m.mask_action}
                      </span>
                    </div>
                    <button
                      onClick={() => del("delete_global_mask", m.id)}
                      className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700 flex-shrink-0 ml-3"
                    >
                      删除
                    </button>
                  </div>
                ))}
                <div className="px-4 py-3">
                  <AddMaskRow
                    label="添加全局脱敏规则"
                    onSubmit={addGlobal}
                    fields={
                      <>
                        <input
                          type="text"
                          value={newGlobal.field_name}
                          onChange={(e) => setNewGlobal((p) => ({ ...p, field_name: e.target.value }))}
                          placeholder="字段名"
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs w-32 focus:outline-none"
                        />
                        <select
                          value={newGlobal.data_domain_id}
                          onChange={(e) => setNewGlobal((p) => ({ ...p, data_domain_id: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          <option value="">全域</option>
                          {domains.map((d) => <option key={d.id} value={d.id}>{d.display_name}</option>)}
                        </select>
                        <select
                          value={newGlobal.mask_action}
                          onChange={(e) => setNewGlobal((p) => ({ ...p, mask_action: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          {MASK_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                        </select>
                      </>
                    }
                  />
                </div>
              </>
            )}

            {/* Role masks */}
            {tab === "role" && (
              <>
                {filteredRoleMasks.length === 0 && (
                  <div className="py-8 text-center text-gray-400 text-xs font-bold uppercase">暂无角色脱敏覆盖规则</div>
                )}
                {filteredRoleMasks.map((m) => (
                  <div key={m.id} className="px-4 py-3 flex items-center justify-between hover:bg-[#F0F4F8]">
                    <div className="flex items-center gap-3 flex-1 min-w-0">
                      <span className="text-[9px] text-[#00A3C4] font-bold uppercase w-20 truncate">{posName(m.position_id)}</span>
                      <span className="text-xs font-bold text-[#1A202C] w-32 truncate">{m.field_name}</span>
                      <span className="text-[9px] text-gray-400 uppercase w-20 truncate">{domainName(m.data_domain_id)}</span>
                      <span className={`border px-2 py-0.5 text-[9px] font-bold uppercase ${MASK_ACTION_COLORS[m.mask_action] || "bg-gray-100 text-gray-600 border-gray-400"}`}>
                        {m.mask_action}
                      </span>
                    </div>
                    <button
                      onClick={() => del("delete_role_mask", m.id)}
                      className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700 flex-shrink-0 ml-3"
                    >
                      删除
                    </button>
                  </div>
                ))}
                <div className="px-4 py-3">
                  <AddMaskRow
                    label="添加角色脱敏覆盖"
                    onSubmit={addRole}
                    fields={
                      <>
                        <select
                          value={newRole.position_id}
                          onChange={(e) => setNewRole((p) => ({ ...p, position_id: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          <option value="">岗位</option>
                          {positions.map((pos) => <option key={pos.id} value={pos.id}>{pos.name}</option>)}
                        </select>
                        <input
                          type="text"
                          value={newRole.field_name}
                          onChange={(e) => setNewRole((p) => ({ ...p, field_name: e.target.value }))}
                          placeholder="字段名"
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs w-28 focus:outline-none"
                        />
                        <select
                          value={newRole.data_domain_id}
                          onChange={(e) => setNewRole((p) => ({ ...p, data_domain_id: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          <option value="">全域</option>
                          {domains.map((d) => <option key={d.id} value={d.id}>{d.display_name}</option>)}
                        </select>
                        <select
                          value={newRole.mask_action}
                          onChange={(e) => setNewRole((p) => ({ ...p, mask_action: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          {MASK_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                        </select>
                      </>
                    }
                  />
                </div>
              </>
            )}

            {/* Output masks */}
            {tab === "output" && (
              <>
                {filteredOutputMasks.length === 0 && (
                  <div className="py-8 text-center text-gray-400 text-xs font-bold uppercase">暂无输出遮罩规则</div>
                )}
                {filteredOutputMasks.map((m) => (
                  <div key={m.id} className="px-4 py-3 flex items-center justify-between hover:bg-[#F0F4F8]">
                    <div className="flex items-center gap-3 flex-1 min-w-0">
                      <span className="text-[9px] text-[#00A3C4] font-bold uppercase w-20 truncate">{posName(m.position_id)}</span>
                      <span className="text-[9px] text-gray-400 uppercase w-20 truncate">{domainName(m.data_domain_id)}</span>
                      <span className="text-xs font-bold text-[#1A202C] w-32 truncate">{m.field_name}</span>
                      <span className={`border px-2 py-0.5 text-[9px] font-bold uppercase ${MASK_ACTION_COLORS[m.mask_action] || "bg-gray-100 text-gray-600 border-gray-400"}`}>
                        {m.mask_action}
                      </span>
                    </div>
                    <button
                      onClick={() => del("delete_output_mask", m.id)}
                      className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700 flex-shrink-0 ml-3"
                    >
                      删除
                    </button>
                  </div>
                ))}
                <div className="px-4 py-3">
                  <AddMaskRow
                    label="添加输出遮罩"
                    onSubmit={addOutput}
                    fields={
                      <>
                        <select
                          value={newOutput.position_id}
                          onChange={(e) => setNewOutput((p) => ({ ...p, position_id: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          <option value="">岗位</option>
                          {positions.map((pos) => <option key={pos.id} value={pos.id}>{pos.name}</option>)}
                        </select>
                        <select
                          value={newOutput.data_domain_id}
                          onChange={(e) => setNewOutput((p) => ({ ...p, data_domain_id: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          <option value="">数据域</option>
                          {domains.map((d) => <option key={d.id} value={d.id}>{d.display_name}</option>)}
                        </select>
                        <input
                          type="text"
                          value={newOutput.field_name}
                          onChange={(e) => setNewOutput((p) => ({ ...p, field_name: e.target.value }))}
                          placeholder="字段名"
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs w-28 focus:outline-none"
                        />
                        <select
                          value={newOutput.mask_action}
                          onChange={(e) => setNewOutput((p) => ({ ...p, mask_action: e.target.value }))}
                          className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
                        >
                          {MASK_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                        </select>
                      </>
                    }
                  />
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {showPreview && (
        <PreviewModal
          positions={positions}
          domains={domains}
          token={token}
          onClose={() => setShowPreview(false)}
        />
      )}
    </div>
  );
}
