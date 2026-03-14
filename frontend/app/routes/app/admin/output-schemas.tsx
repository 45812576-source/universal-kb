import { useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/output-schemas";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Skill } from "~/lib/types";

// ─── Types ────────────────────────────────────────────────────────────────────

interface OutputSchema {
  id: number;
  skill_id: number;
  version: number;
  status: "pending_review" | "approved";
  schema_json: Record<string, unknown>;
  created_by: number | null;
  approved_by: number | null;
  created_at: string;
}

// ─── Loader ───────────────────────────────────────────────────────────────────

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const [skills, schemas] = await Promise.all([
    apiFetch("/api/skills", { token }),
    apiFetch("/api/admin/output-schemas", { token }),
  ]);
  return { skills, schemas, currentUser: user, token };
}

// ─── Action ───────────────────────────────────────────────────────────────────

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const body = await request.json();
  const { intent, schema_id, skill_id, ...rest } = body as {
    intent: string;
    schema_id?: number;
    skill_id?: number;
    [k: string]: unknown;
  };

  if (intent === "generate") {
    await apiFetch(`/api/admin/output-schemas/generate?skill_id=${skill_id}`, {
      method: "POST",
      token,
    });
  } else if (intent === "approve") {
    await apiFetch(`/api/admin/output-schemas/${schema_id}/approve`, {
      method: "POST",
      token,
    });
  } else if (intent === "update") {
    await apiFetch(`/api/admin/output-schemas/${schema_id}`, {
      method: "PUT",
      body: JSON.stringify(rest),
      token,
    });
  }
  return null;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmt(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// ─── Schema Detail Modal ──────────────────────────────────────────────────────

function SchemaModal({
  schema,
  isSuperAdmin,
  onClose,
}: {
  schema: OutputSchema;
  isSuperAdmin: boolean;
  onClose: () => void;
}) {
  const fetcher = useFetcher();
  const [editing, setEditing] = useState(false);
  const [jsonText, setJsonText] = useState(JSON.stringify(schema.schema_json, null, 2));
  const [jsonError, setJsonError] = useState("");

  const isPending = schema.status === "pending_review";

  function saveEdit() {
    try {
      const parsed = JSON.parse(jsonText);
      fetcher.submit(
        { intent: "update", schema_id: schema.id, schema_json: parsed },
        { method: "POST", encType: "application/json" },
      );
      setEditing(false);
      setJsonError("");
    } catch {
      setJsonError("JSON 格式错误");
    }
  }

  function approve() {
    fetcher.submit(
      { intent: "approve", schema_id: schema.id },
      { method: "POST", encType: "application/json" },
    );
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white pixel-border w-full max-w-xl flex flex-col max-h-[90vh]">
        <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C] flex-shrink-0">
          <span className="text-[10px] font-bold uppercase tracking-widest">
            Output Schema v{schema.version} — Skill #{schema.skill_id}
          </span>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-xs font-bold">✕</button>
        </div>

        <div className="overflow-y-auto flex-1 p-4 space-y-4">
          {/* Status */}
          <div className="flex items-center gap-3">
            <span className={`border px-2 py-0.5 text-[9px] font-bold uppercase ${
              schema.status === "approved"
                ? "bg-green-100 text-green-700 border-green-400"
                : "bg-yellow-100 text-yellow-700 border-yellow-400"
            }`}>
              {schema.status === "approved" ? "已审批" : "待审批"}
            </span>
            <span className="text-[9px] text-gray-400">创建于 {fmt(schema.created_at)}</span>
          </div>

          {/* Schema JSON */}
          {editing ? (
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-1">编辑 Schema JSON</div>
              <textarea
                value={jsonText}
                onChange={(e) => setJsonText(e.target.value)}
                rows={12}
                className="w-full text-xs border-2 border-[#1A202C] px-2 py-1.5 font-mono resize-none focus:outline-none focus:border-[#00A3C4]"
              />
              {jsonError && <div className="text-xs text-red-600 font-bold mt-1">{jsonError}</div>}
              <div className="flex gap-2 mt-2">
                <button
                  onClick={saveEdit}
                  className="bg-[#1A202C] text-white px-3 py-1.5 text-[10px] font-bold uppercase hover:bg-black"
                >
                  保存
                </button>
                <button
                  onClick={() => { setEditing(false); setJsonError(""); }}
                  className="border-2 border-[#1A202C] px-3 py-1.5 text-[10px] font-bold uppercase hover:bg-[#F0F4F8]"
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <div>
              <div className="flex items-center justify-between mb-1">
                <div className="text-[9px] font-bold uppercase text-gray-400">Schema JSON</div>
                {isPending && (
                  <button
                    onClick={() => setEditing(true)}
                    className="text-[9px] font-bold uppercase text-[#00A3C4] hover:underline"
                  >
                    编辑
                  </button>
                )}
              </div>
              <pre className="bg-[#F0F4F8] border-2 border-[#1A202C] p-3 text-xs font-mono overflow-x-auto max-h-64">
                {JSON.stringify(schema.schema_json, null, 2)}
              </pre>
            </div>
          )}

          {/* Actions */}
          {isSuperAdmin && isPending && !editing && (
            <div className="border-t-2 border-[#1A202C] pt-3">
              <button
                onClick={approve}
                className="w-full bg-green-600 text-white px-4 py-2 text-[10px] font-bold uppercase hover:bg-green-700"
              >
                审批通过
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function OutputSchemasPage() {
  const { skills, schemas, currentUser } = useLoaderData<typeof loader>() as {
    skills: Skill[];
    schemas: OutputSchema[];
    currentUser: { role: string };
  };

  const fetcher = useFetcher();
  const [selected, setSelected] = useState<OutputSchema | null>(null);
  const [genSkillId, setGenSkillId] = useState("");
  const [filterStatus, setFilterStatus] = useState("");

  const isSuperAdmin = currentUser.role === "super_admin";

  const filtered = schemas.filter((s) => {
    if (filterStatus && s.status !== filterStatus) return false;
    return true;
  });

  const pendingCount = schemas.filter((s) => s.status === "pending_review").length;

  function generateSchema() {
    if (!genSkillId) return;
    fetcher.submit(
      { intent: "generate", skill_id: parseInt(genSkillId) },
      { method: "POST", encType: "application/json" },
    );
    setGenSkillId("");
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">Output Schema 管理</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">
              Skill 输出结构定义 · LLM 推导 + 人工审批
              {pendingCount > 0 && (
                <span className="ml-2 bg-yellow-400 text-yellow-900 px-1.5 py-0.5 text-[8px]">
                  {pendingCount} 待审批
                </span>
              )}
            </p>
          </div>
        </div>
        {/* Generate trigger */}
        <div className="flex items-center gap-2">
          <select
            value={genSkillId}
            onChange={(e) => setGenSkillId(e.target.value)}
            className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none"
          >
            <option value="">选择 Skill</option>
            {skills.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <button
            onClick={generateSchema}
            disabled={!genSkillId || fetcher.state !== "idle"}
            className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase hover:bg-black disabled:opacity-50"
          >
            {fetcher.state !== "idle" ? "推导中..." : "LLM 推导"}
          </button>
        </div>
      </div>

      <div className="p-6 max-w-5xl space-y-4">
        {/* Filter */}
        <div className="flex items-center gap-3">
          <div className="text-[9px] font-bold uppercase text-gray-500">状态筛选：</div>
          {(["", "pending_review", "approved"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setFilterStatus(s)}
              className={`px-3 py-1 text-[10px] font-bold uppercase border-2 ${
                filterStatus === s
                  ? "bg-[#1A202C] text-white border-[#1A202C]"
                  : "border-[#1A202C] text-[#1A202C] hover:bg-[#CCF2FF]"
              }`}
            >
              {s === "" ? "全部" : s === "pending_review" ? "待审批" : "已审批"}
            </button>
          ))}
        </div>

        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Output_Schema_Registry</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          {filtered.length === 0 ? (
            <div className="py-16 text-center text-gray-400 text-xs font-bold uppercase">暂无 Schema 记录</div>
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="border-b-2 border-[#1A202C] bg-[#F0F4F8]">
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">Skill</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">版本</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">字段数</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">创建时间</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {filtered.map((s) => {
                  const skill = skills.find((sk) => sk.id === s.skill_id);
                  const fieldCount = Object.keys(s.schema_json).length;
                  return (
                    <tr key={s.id} className="hover:bg-[#F0F4F8] transition-colors">
                      <td className="py-3 px-4">
                        <div className="text-xs font-bold text-[#1A202C]">{skill?.name || `Skill #${s.skill_id}`}</div>
                      </td>
                      <td className="py-3 px-4 text-xs font-bold text-gray-500">v{s.version}</td>
                      <td className="py-3 px-4">
                        <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${
                          s.status === "approved"
                            ? "bg-green-100 text-green-700 border-green-400"
                            : "bg-yellow-100 text-yellow-700 border-yellow-400"
                        }`}>
                          {s.status === "approved" ? "已审批" : "待审批"}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-xs text-gray-500">{fieldCount} 个字段</td>
                      <td className="py-3 px-4 text-[10px] text-gray-500">{fmt(s.created_at)}</td>
                      <td className="py-3 px-4 text-right">
                        <button
                          onClick={() => setSelected(s)}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          {s.status === "pending_review" && isSuperAdmin ? "审批" : "查看"}
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
        <SchemaModal
          schema={selected}
          isSuperAdmin={isSuperAdmin}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}
