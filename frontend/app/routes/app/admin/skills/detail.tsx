import { useEffect, useState } from "react";
import { Form, Link, redirect, useActionData, useLoaderData, useNavigation, useParams } from "react-router";
import type { Route } from "./+types/detail";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";
import type { ModelConfig } from "~/lib/types";

const SUGGESTION_STATUS: Record<string, { label: string; color: string }> = {
  pending:  { label: "待审核",   color: "bg-gray-100 text-gray-600 border-gray-400" },
  adopted:  { label: "已采纳",   color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
  partial:  { label: "部分采纳", color: "bg-blue-100 text-blue-700 border-blue-400" },
  rejected: { label: "未采纳",   color: "bg-red-100 text-red-600 border-red-400" },
};

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
      {children}
    </label>
  );
}

function PixelInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${props.className || ""}`}
    />
  );
}

function PixelSelect({ children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${(props as any).className || ""}`}
    >
      {children}
    </select>
  );
}

// --- Suggestions Tab ---
function SuggestionsTab({ skillId, token, isAdmin }: { skillId: number; token: string; isAdmin: boolean }) {
  const [suggestions, setSuggestions] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [iterating, setIterating] = useState(false);
  const [iterPreview, setIterPreview] = useState<any>(null);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");
  const [reviewingId, setReviewingId] = useState<number | null>(null);
  const [reviewForm, setReviewForm] = useState({ status: "adopted", note: "" });

  async function load() {
    setLoading(true);
    try {
      const data = await apiFetch(`/api/skills/${skillId}/suggestions`, { token });
      setSuggestions(data || []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [skillId]);

  async function submitReview(id: number) {
    try {
      await apiFetch(`/api/skill-suggestions/${id}/review`, {
        method: "PATCH",
        body: JSON.stringify({ status: reviewForm.status, review_note: reviewForm.note }),
        token,
      });
      setReviewingId(null);
      load();
    } catch (e: any) {
      setError(e.message || "审核失败");
    }
  }

  async function triggerIterate() {
    if (selected.size === 0) { setError("请先选择要采纳的意见"); return; }
    setIterating(true);
    setError("");
    setIterPreview(null);
    try {
      const preview = await apiFetch(`/api/skills/${skillId}/iterate`, {
        method: "POST",
        body: JSON.stringify({ suggestion_ids: Array.from(selected) }),
        token,
      });
      setIterPreview(preview);
    } catch (e: any) {
      setError(e.message || "迭代生成失败");
    } finally {
      setIterating(false);
    }
  }

  async function applyIterate() {
    if (!iterPreview) return;
    setApplying(true);
    setError("");
    try {
      await apiFetch(`/api/skills/${skillId}/iterate/apply`, {
        method: "POST",
        body: JSON.stringify({
          proposed: iterPreview.proposed,
          change_note: iterPreview.change_note,
          suggestion_ids: iterPreview.suggestion_ids,
        }),
        token,
      });
      setIterPreview(null);
      setSelected(new Set());
      window.location.reload();
    } catch (e: any) {
      setError(e.message || "应用失败");
    } finally {
      setApplying(false);
    }
  }

  const adoptable = suggestions.filter((s) => s.status === "adopted" || s.status === "partial");

  return (
    <div className="space-y-4">
      {error && (
        <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-700 uppercase">
          [ERROR] {error}
        </div>
      )}

      {isAdmin && adoptable.length > 0 && (
        <div className="border-2 border-[#00D1FF] bg-[#CCF2FF]/20 p-4 space-y-3">
          <p className="text-xs font-bold uppercase text-[#00A3C4]">— 基于已采纳意见迭代版本</p>
          <p className="text-[10px] font-bold uppercase text-gray-500">勾选下方已采纳/部分采纳的意见，生成新版本预览</p>
          <button
            onClick={triggerIterate}
            disabled={iterating || selected.size === 0}
            className="bg-[#1A202C] text-[#00D1FF] px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
          >
            {iterating ? "AI 生成中..." : `基于 ${selected.size} 条意见迭代 >`}
          </button>
        </div>
      )}

      {iterPreview && (
        <div className="border-2 border-green-400 bg-green-50 p-4 space-y-3">
          <p className="text-xs font-bold uppercase text-green-700">迭代预览 — v{iterPreview.current_version + 1}</p>
          {iterPreview.diff?.system_prompt && (
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="border-2 border-red-300 bg-red-50 p-2 font-mono whitespace-pre-wrap max-h-40 overflow-y-auto">
                <p className="font-bold text-red-500 mb-1 uppercase text-[9px]">旧 Prompt</p>
                {String(iterPreview.diff.system_prompt.old).slice(0, 400)}
              </div>
              <div className="border-2 border-green-300 bg-green-50 p-2 font-mono whitespace-pre-wrap max-h-40 overflow-y-auto">
                <p className="font-bold text-green-600 mb-1 uppercase text-[9px]">新 Prompt</p>
                {String(iterPreview.diff.system_prompt.new).slice(0, 400)}
              </div>
            </div>
          )}
          <p className="text-[10px] font-bold text-gray-500 uppercase">变更说明：{iterPreview.change_note}</p>
          <div className="flex gap-3">
            <button
              onClick={applyIterate}
              disabled={applying}
              className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
            >
              {applying ? "发布中..." : "确认发布新版本"}
            </button>
            <button
              onClick={() => setIterPreview(null)}
              className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
            >
              重新生成
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <p className="text-xs font-bold uppercase text-gray-400">加载中...</p>
      ) : suggestions.length === 0 ? (
        <p className="text-xs font-bold uppercase text-gray-400">暂无改进建议</p>
      ) : (
        <div className="space-y-2">
          {suggestions.map((s: any) => {
            const si = SUGGESTION_STATUS[s.status] ?? SUGGESTION_STATUS.pending;
            const isAdoptable = s.status === "adopted" || s.status === "partial";
            return (
              <div key={s.id} className={`border-2 p-4 ${isAdoptable ? "border-[#00D1FF] bg-[#CCF2FF]/10" : "border-[#1A202C] bg-white"}`}>
                <div className="flex items-start gap-3">
                  {isAdmin && isAdoptable && (
                    <input
                      type="checkbox"
                      checked={selected.has(s.id)}
                      onChange={(e) => {
                        const next = new Set(selected);
                        e.target.checked ? next.add(s.id) : next.delete(s.id);
                        setSelected(next);
                      }}
                      className="mt-1 h-4 w-4 border-2 border-[#1A202C]"
                    />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[9px] font-bold uppercase text-gray-400">{s.submitter_name ?? `用户#${s.submitted_by}`}</span>
                      <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${si.color}`}>
                        {si.label}
                      </span>
                      <span className="text-[9px] font-bold uppercase text-gray-400">
                        {s.created_at ? new Date(s.created_at).toLocaleDateString("zh-CN") : ""}
                      </span>
                    </div>
                    <p className="text-xs font-bold text-[#1A202C] mb-1">{s.problem_desc}</p>
                    <p className="text-[10px] text-gray-600">{s.expected_direction}</p>
                    {s.case_example && (
                      <p className="mt-1 text-[10px] text-gray-400 italic line-clamp-2">案例：{s.case_example}</p>
                    )}
                    {s.review_note && (
                      <div className="mt-2 text-[10px] font-bold uppercase text-[#00A3C4] border-2 border-[#00D1FF] bg-[#CCF2FF]/20 px-2 py-1">
                        [负责人] {s.review_note}
                      </div>
                    )}
                    {isAdmin && s.status === "pending" && (
                      reviewingId === s.id ? (
                        <div className="mt-2 flex items-center gap-2 flex-wrap">
                          <select
                            value={reviewForm.status}
                            onChange={(e) => setReviewForm((f) => ({ ...f, status: e.target.value }))}
                            className="border-2 border-[#1A202C] bg-white px-2 py-1 text-[10px] font-bold uppercase focus:outline-none"
                          >
                            <option value="adopted">采纳</option>
                            <option value="partial">部分采纳</option>
                            <option value="rejected">不采纳</option>
                          </select>
                          <input
                            value={reviewForm.note}
                            onChange={(e) => setReviewForm((f) => ({ ...f, note: e.target.value }))}
                            placeholder="附言（可选）"
                            className="flex-1 border-2 border-[#1A202C] bg-white px-2 py-1 text-[10px] font-bold focus:outline-none focus:border-[#00D1FF]"
                          />
                          <button
                            onClick={() => submitReview(s.id)}
                            className="bg-[#1A202C] text-white px-2 py-1 text-[9px] font-bold uppercase hover:bg-black"
                          >
                            确认
                          </button>
                          <button
                            onClick={() => setReviewingId(null)}
                            className="text-[9px] font-bold uppercase text-gray-400"
                          >
                            取消
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => { setReviewingId(s.id); setReviewForm({ status: "adopted", note: "" }); }}
                          className="mt-2 text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          标记审核结果 &gt;
                        </button>
                      )
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// --- AI Edit Panel ---
function AiEditPanel({ skillId, token, onApplied }: { skillId: number; token: string; onApplied: () => void }) {
  const [instruction, setInstruction] = useState("");
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<any>(null);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");

  async function generatePreview() {
    if (!instruction.trim()) return;
    setLoading(true);
    setError("");
    setPreview(null);
    try {
      const result = await apiFetch(`/api/skills/${skillId}/edit-with-ai`, {
        method: "POST",
        body: JSON.stringify({ instruction }),
        token,
      });
      setPreview(result);
    } catch (e: any) {
      setError(e.message || "生成失败");
    } finally {
      setLoading(false);
    }
  }

  async function applyEdit() {
    if (!preview) return;
    setApplying(true);
    setError("");
    try {
      await apiFetch(`/api/skills/${skillId}/edit-with-ai/apply`, {
        method: "POST",
        body: JSON.stringify({ proposed: preview.proposed, change_note: preview.change_note }),
        token,
      });
      setPreview(null);
      setInstruction("");
      onApplied();
    } catch (e: any) {
      setError(e.message || "应用失败");
    } finally {
      setApplying(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <FieldLabel>修改指令</FieldLabel>
        <textarea
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          rows={3}
          placeholder="例：加入竞品分析模块，要求对比3个竞品的核心指标..."
          className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-none"
        />
      </div>
      <button
        onClick={generatePreview}
        disabled={loading || !instruction.trim()}
        className="bg-[#1A202C] text-[#00D1FF] px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
      >
        {loading ? "AI 生成中..." : "生成修改预览"}
      </button>

      {error && (
        <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-700 uppercase">
          [ERROR] {error}
        </div>
      )}

      {preview && (
        <div className="space-y-4">
          <p className="text-xs font-bold uppercase text-gray-700">修改预览</p>
          {Object.keys(preview.diff || {}).length === 0 ? (
            <p className="text-xs font-bold uppercase text-gray-400">无变更</p>
          ) : (
            <div className="space-y-3">
              {preview.diff?.system_prompt && (
                <div>
                  <p className="text-[10px] font-bold uppercase text-gray-500 mb-1">System Prompt 变更</p>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="border-2 border-red-300 bg-red-50 p-3 font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
                      <p className="font-bold text-red-500 mb-1 uppercase text-[9px]">旧</p>
                      {preview.diff.system_prompt.old}
                    </div>
                    <div className="border-2 border-green-300 bg-green-50 p-3 font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
                      <p className="font-bold text-green-600 mb-1 uppercase text-[9px]">新</p>
                      {preview.diff.system_prompt.new}
                    </div>
                  </div>
                </div>
              )}
              {preview.diff?.variables && (
                <div>
                  <p className="text-[10px] font-bold uppercase text-gray-500 mb-1">变量变更</p>
                  <div className="text-xs font-bold text-gray-600">
                    旧: {JSON.stringify(preview.diff.variables.old)} → 新: {JSON.stringify(preview.diff.variables.new)}
                  </div>
                </div>
              )}
            </div>
          )}
          <p className="text-[10px] font-bold uppercase text-gray-500">变更说明：{preview.change_note}</p>
          <p className="text-[9px] font-bold uppercase text-gray-400">确认后将创建 v{preview.current_version + 1}</p>
          <div className="flex gap-3">
            <button
              onClick={applyEdit}
              disabled={applying}
              className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
            >
              {applying ? "应用中..." : "确认创建新版本"}
            </button>
            <button
              onClick={() => setPreview(null)}
              className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
            >
              重新修改
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// --- Tools Tab ---
function ToolsTab({ skillId, token, isAdmin }: { skillId: number; token: string; isAdmin: boolean }) {
  const [boundTools, setBoundTools] = useState<any[]>([]);
  const [allTools, setAllTools] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    try {
      const [bound, all] = await Promise.all([
        apiFetch(`/api/tools/skill/${skillId}/tools`, { token }),
        apiFetch("/api/tools", { token }),
      ]);
      setBoundTools(bound || []);
      setAllTools(all || []);
    } catch (e: any) {
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [skillId]);

  async function bindTool(toolId: number) {
    try {
      await apiFetch(`/api/tools/skill/${skillId}/tools/${toolId}`, { method: "POST", token });
      load();
    } catch (e: any) {
      setError(e.message || "绑定失败");
    }
  }

  async function unbindTool(toolId: number) {
    try {
      await apiFetch(`/api/tools/skill/${skillId}/tools/${toolId}`, { method: "DELETE", token });
      load();
    } catch (e: any) {
      setError(e.message || "解绑失败");
    }
  }

  const boundIds = new Set(boundTools.map((t: any) => t.id));
  const availableTools = allTools.filter((t: any) => !boundIds.has(t.id) && t.is_active);

  return (
    <div className="space-y-6">
      {error && (
        <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-700 uppercase">[ERROR] {error}</div>
      )}
      {loading ? (
        <p className="text-xs font-bold uppercase text-gray-400">加载中...</p>
      ) : (
        <>
          <div>
            <p className="text-[10px] font-bold uppercase tracking-widest text-[#00A3C4] mb-3">— 已绑定工具</p>
            {boundTools.length === 0 ? (
              <p className="text-xs font-bold uppercase text-gray-400">尚未绑定任何工具</p>
            ) : (
              <div className="space-y-2">
                {boundTools.map((tool: any) => (
                  <div key={tool.id} className="flex items-center justify-between border-2 border-[#1A202C] bg-white p-3">
                    <div>
                      <span className="text-xs font-bold text-[#1A202C]">{tool.display_name}</span>
                      <span className="ml-2 text-[9px] font-bold uppercase text-gray-400">{tool.name}</span>
                    </div>
                    {isAdmin && (
                      <button onClick={() => unbindTool(tool.id)} className="text-[10px] font-bold uppercase text-red-500 hover:underline">
                        解绑
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
          {isAdmin && availableTools.length > 0 && (
            <div>
              <p className="text-[10px] font-bold uppercase tracking-widest text-[#00A3C4] mb-3">— 添加工具</p>
              <div className="space-y-2">
                {availableTools.map((tool: any) => (
                  <div key={tool.id} className="flex items-center justify-between border-2 border-dashed border-[#1A202C] bg-[#F0F4F8] p-3">
                    <div>
                      <span className="text-xs font-bold text-[#1A202C]">{tool.display_name}</span>
                      <span className="ml-2 text-[9px] font-bold uppercase text-gray-400">{tool.name}</span>
                      {tool.description && <p className="text-[9px] text-gray-400 mt-0.5">{tool.description}</p>}
                    </div>
                    <button onClick={() => bindTool(tool.id)} className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">
                      + 绑定
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// --- Upstream Diff Tab ---
function UpstreamTab({ skillId, token }: { skillId: number; token: string }) {
  const [diff, setDiff] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  async function load() {
    setLoading(true);
    try {
      const data = await apiFetch(`/api/skills/${skillId}/upstream-diff`, { token });
      setDiff(data);
    } catch (e: any) {
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [skillId]);

  async function syncAction(action: "overwrite" | "ignore") {
    setSyncing(true);
    setError("");
    try {
      await apiFetch(`/api/skills/${skillId}/upstream-sync`, {
        method: "POST",
        body: JSON.stringify({ action }),
        token,
      });
      setSuccess(action === "overwrite" ? "已同步上游最新版本，新版本已创建" : "已忽略本次上游更新");
      load();
    } catch (e: any) {
      setError(e.message || "操作失败");
    } finally {
      setSyncing(false);
    }
  }

  if (loading) return <p className="text-xs font-bold uppercase text-gray-400">加载中...</p>;

  if (!diff?.has_upstream) {
    return (
      <div className="border-2 border-dashed border-gray-300 p-8 text-center">
        <p className="text-xs font-bold uppercase text-gray-400">此 Skill 非从外部市场导入，无上游对比</p>
      </div>
    );
  }

  const oldLines = diff.upstream_content ? diff.upstream_content.split("\n") : [];
  const newLines = diff.local_content ? diff.local_content.split("\n") : [];
  const oldSet = new Set(oldLines);
  const newSet = new Set(newLines);

  return (
    <div className="space-y-5">
      {error && (
        <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-700 uppercase">[ERROR] {error}</div>
      )}
      {success && (
        <div className="border-2 border-[#00D1FF] bg-[#CCF2FF]/30 px-3 py-2 text-xs font-bold text-[#00A3C4] uppercase">[OK] {success}</div>
      )}

      {/* Status banner */}
      <div className="border-2 border-[#1A202C] bg-white p-4 flex items-center justify-between">
        <div>
          <p className="text-[10px] font-bold uppercase text-gray-500">上游版本</p>
          <p className="text-xs font-bold text-[#1A202C]">v{diff.upstream_version}</p>
          <p className="text-[9px] text-gray-400 mt-0.5">
            上次同步：{diff.upstream_synced_at ? new Date(diff.upstream_synced_at).toLocaleDateString("zh-CN") : "—"}
          </p>
        </div>
        <div className="text-right">
          {diff.is_customized && (
            <span className="inline-block border px-2 py-0.5 text-[9px] font-bold uppercase bg-yellow-100 text-yellow-700 border-yellow-400">
              已二次修改
            </span>
          )}
          {diff.has_new_upstream && (
            <div className="mt-1">
              <span className="inline-block border px-2 py-0.5 text-[9px] font-bold uppercase bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]">
                上游有新版本 v{diff.new_upstream_version}
              </span>
              {diff.diff_summary && (
                <p className="text-[9px] text-gray-400 mt-0.5">{diff.diff_summary}</p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Sync actions */}
      {diff.has_new_upstream && diff.check_action === "pending" && (
        <div className="border-2 border-[#00D1FF] bg-[#CCF2FF]/20 p-4 space-y-3">
          <p className="text-[10px] font-bold uppercase text-[#00A3C4]">— 同步决策</p>
          <div className="flex gap-3">
            <button
              onClick={() => syncAction("overwrite")}
              disabled={syncing}
              className="bg-[#1A202C] text-[#00D1FF] px-4 py-2 text-[10px] font-bold uppercase hover:bg-black disabled:opacity-50 pixel-border"
            >
              {syncing ? "处理中..." : "覆盖 — 拉取上游最新版"}
            </button>
            <button
              onClick={() => syncAction("ignore")}
              disabled={syncing}
              className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100 disabled:opacity-50"
            >
              忽略 — 保持本地版本
            </button>
          </div>
        </div>
      )}

      {/* Diff view */}
      <div>
        <p className="text-[10px] font-bold uppercase text-gray-500 mb-2">上游原版 vs 本地版本</p>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <p className="text-[9px] font-bold uppercase text-gray-400 mb-1">上游原版（导入时快照）</p>
            <div className="border-2 border-[#1A202C] bg-[#F8FAFC] p-3 font-mono text-xs max-h-80 overflow-y-auto">
              {oldLines.map((line: string, i: number) => (
                <div
                  key={i}
                  className={`whitespace-pre-wrap leading-5 ${!newSet.has(line) ? "bg-red-100 text-red-700" : ""}`}
                >
                  {line || "\u00A0"}
                </div>
              ))}
            </div>
          </div>
          <div>
            <p className="text-[9px] font-bold uppercase text-gray-400 mb-1">本地版本（现在）</p>
            <div className="border-2 border-[#1A202C] bg-[#F8FAFC] p-3 font-mono text-xs max-h-80 overflow-y-auto">
              {newLines.map((line: string, i: number) => (
                <div
                  key={i}
                  className={`whitespace-pre-wrap leading-5 ${!oldSet.has(line) ? "bg-green-100 text-green-700" : ""}`}
                >
                  {line || "\u00A0"}
                </div>
              ))}
            </div>
          </div>
        </div>
        <p className="text-[9px] text-gray-400 mt-1">红色 = 上游有但本地已删 / 绿色 = 本地新增</p>
      </div>
    </div>
  );
}

export async function loader({ request, params }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const models = await apiFetch("/api/admin/models", { token });
  if (params.id === "new") {
    return { skill: null, models, token, user };
  }
  const skill = await apiFetch(`/api/skills/${params.id}`, { token });
  return { skill, models, token, user };
}

export async function action({ request, params }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;

  if (intent === "save") {
    const body = {
      name: form.get("name") as string,
      description: form.get("description") as string,
      mode: form.get("mode") as string,
      knowledge_tags: (form.get("knowledge_tags") as string).split(",").map((t) => t.trim()).filter(Boolean),
      auto_inject: form.get("auto_inject") === "true",
      system_prompt: form.get("system_prompt") as string,
      variables: (form.get("variables") as string).split(",").map((v) => v.trim()).filter(Boolean),
      model_config_id: form.get("model_config_id") ? Number(form.get("model_config_id")) : null,
    };
    try {
      if (params.id === "new") {
        const result = await apiFetch("/api/skills", { method: "POST", body: JSON.stringify(body), token });
        return redirect(`/admin/skills/${result.id}`);
      } else {
        await apiFetch(`/api/skills/${params.id}`, { method: "PUT", body: JSON.stringify(body), token });
        return { success: "保存成功" };
      }
    } catch (e) {
      if (e instanceof ApiError) return { error: e.message };
      return { error: "保存失败" };
    }
  }

  if (intent === "new_version") {
    try {
      await apiFetch(`/api/skills/${params.id}/versions`, {
        method: "POST",
        body: JSON.stringify({
          system_prompt: form.get("system_prompt") as string,
          variables: (form.get("variables") as string).split(",").map((v) => v.trim()).filter(Boolean),
          model_config_id: form.get("model_config_id") ? Number(form.get("model_config_id")) : null,
          change_note: form.get("change_note") as string,
        }),
        token,
      });
      return { success: "新版本已创建" };
    } catch (e) {
      if (e instanceof ApiError) return { error: e.message };
      return { error: "创建版本失败" };
    }
  }

  return null;
}

const SKILL_STATUS_STYLE: Record<string, string> = {
  draft:     "bg-gray-100 text-gray-600 border-gray-400",
  reviewing: "bg-yellow-100 text-yellow-700 border-yellow-400",
  published: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]",
  archived:  "bg-red-100 text-red-600 border-red-400",
};
const SKILL_STATUS_LABEL: Record<string, string> = {
  draft: "草稿", reviewing: "审核中", published: "已发布", archived: "已归档",
};

export default function SkillDetail() {
  const { skill, models, token, user } = useLoaderData<typeof loader>() as {
    skill: any;
    models: ModelConfig[];
    token: string;
    user: any;
  };
  const actionData = useActionData<typeof action>() as any;
  const navigation = useNavigation();
  const params = useParams();
  const isNew = params.id === "new";
  const isSaving = navigation.state !== "idle";

  const latestVersion = skill?.versions?.[0];
  const isAdmin = user?.role === "super_admin" || user?.role === "dept_admin";

  const [showVersionModal, setShowVersionModal] = useState(false);
  const [changeNote, setChangeNote] = useState("");
  const [showAiEdit, setShowAiEdit] = useState(false);
  const [activeTab, setActiveTab] = useState<"info" | "suggestions" | "tools" | "upstream">("info");

  useEffect(() => {
    if (actionData?.success) setShowVersionModal(false);
  }, [actionData]);

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <Link to="/admin/skills" className="text-[10px] font-bold uppercase text-gray-400 hover:text-[#1A202C]">
          &lt; 返回列表
        </Link>
        <span className="text-gray-300">/</span>
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">
          {isNew ? "新建 Skill" : skill?.name}
        </h1>
        {!isNew && skill?.status && (
          <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${SKILL_STATUS_STYLE[skill.status] || "bg-gray-100 text-gray-600 border-gray-400"}`}>
            {SKILL_STATUS_LABEL[skill.status] || skill.status}
          </span>
        )}
      </div>

      <div className="p-6 max-w-4xl">
        {actionData?.error && (
          <div className="mb-4 border-2 border-red-400 bg-red-50 px-4 py-3 text-xs font-bold text-red-700 uppercase">
            [ERROR] {actionData.error}
          </div>
        )}
        {actionData?.success && (
          <div className="mb-4 border-2 border-[#00D1FF] bg-[#CCF2FF]/30 px-4 py-3 text-xs font-bold text-[#00A3C4] uppercase">
            [OK] {actionData.success}
          </div>
        )}

        {/* Tab switcher */}
        {!isNew && (
          <div className="flex gap-0 mb-6 border-2 border-[#1A202C] w-fit">
            {(["info", "suggestions", "tools", "upstream"] as const).map((tab, i) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-5 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors ${i > 0 ? "border-l-2 border-[#1A202C]" : ""} ${
                  activeTab === tab ? "bg-[#1A202C] text-white" : "bg-white text-gray-500 hover:bg-[#EBF4F7]"
                }`}
              >
                {tab === "info" ? "基本信息" : tab === "suggestions" ? "改进意见" : tab === "tools" ? "绑定工具" : "与上游对比"}
              </button>
            ))}
          </div>
        )}

        {/* Suggestions Tab */}
        {!isNew && activeTab === "suggestions" && (
          <SuggestionsTab skillId={skill.id} token={token} isAdmin={isAdmin} />
        )}

        {/* Tools Tab */}
        {!isNew && activeTab === "tools" && (
          <ToolsTab skillId={skill.id} token={token} isAdmin={isAdmin} />
        )}

        {/* Upstream Tab */}
        {!isNew && activeTab === "upstream" && (
          <UpstreamTab skillId={skill.id} token={token} />
        )}

        {/* Info Tab */}
        {(isNew || activeTab === "info") && (
          <>
            <Form method="post" className="space-y-5">
              <input type="hidden" name="intent" value="save" />

              {/* Basic info */}
              <div className="pixel-border bg-white p-5 space-y-4">
                <div className="bg-[#2D3748] text-white px-4 py-2 -mx-5 -mt-5 mb-4 flex items-center gap-2 border-b-2 border-[#1A202C]">
                  <span className="text-[10px] font-bold uppercase tracking-widest">基本信息</span>
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <FieldLabel>名称 <span className="text-[#00D1FF]">*</span></FieldLabel>
                    <PixelInput name="name" required defaultValue={skill?.name ?? ""} placeholder="例: 电商数据分析助手" />
                  </div>
                  <div>
                    <FieldLabel>模式</FieldLabel>
                    <PixelSelect name="mode" defaultValue={skill?.mode ?? "hybrid"}>
                      <option value="structured">结构化（精确计算）</option>
                      <option value="unstructured">非结构化（语义检索）</option>
                      <option value="hybrid">混合</option>
                    </PixelSelect>
                  </div>
                </div>
                <div>
                  <FieldLabel>描述</FieldLabel>
                  <PixelInput name="description" defaultValue={skill?.description ?? ""} placeholder="简单描述这个Skill的用途" />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <FieldLabel>知识标签（逗号分隔）</FieldLabel>
                    <PixelInput name="knowledge_tags" defaultValue={(skill?.knowledge_tags ?? []).join(", ")} placeholder="电商, ROI, 投放" />
                  </div>
                  <div>
                    <FieldLabel>自动注入知识库</FieldLabel>
                    <PixelSelect name="auto_inject" defaultValue={skill?.auto_inject === false ? "false" : "true"}>
                      <option value="true">是</option>
                      <option value="false">否</option>
                    </PixelSelect>
                  </div>
                </div>
              </div>

              {/* Prompt editor */}
              <div className="pixel-border bg-white p-5 space-y-4">
                <div className="bg-[#2D3748] text-white px-4 py-2 -mx-5 -mt-5 mb-4 flex items-center justify-between border-b-2 border-[#1A202C]">
                  <span className="text-[10px] font-bold uppercase tracking-widest">
                    System Prompt {latestVersion && <span className="text-[#00D1FF] ml-2">v{latestVersion.version}</span>}
                  </span>
                  <div className="flex space-x-1.5">
                    <div className="w-2 h-2 bg-red-400" />
                    <div className="w-2 h-2 bg-yellow-400" />
                    <div className="w-2 h-2 bg-green-400" />
                  </div>
                </div>
                <div>
                  <FieldLabel>Prompt 内容（支持变量：{"{{variable_name}}"}）</FieldLabel>
                  {latestVersion && !("system_prompt" in latestVersion) ? (
                    <div className="border-2 border-dashed border-gray-300 bg-gray-50 px-4 py-6 text-center">
                      <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">
                        [仅超级管理员可见]
                      </p>
                    </div>
                  ) : (
                    <textarea
                      name="system_prompt"
                      rows={12}
                      defaultValue={(latestVersion as any)?.system_prompt ?? ""}
                      required
                      className="w-full border-2 border-[#1A202C] bg-[#F8FAFC] px-3 py-2.5 text-xs font-mono font-bold focus:outline-none focus:border-[#00D1FF] resize-y"
                      placeholder={"你是一个专业的Martech顾问...\n\n用户问题：{{user_question}}"}
                    />
                  )}
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <FieldLabel>变量列表（逗号分隔）</FieldLabel>
                    <PixelInput name="variables" defaultValue={(latestVersion?.variables ?? []).join(", ")} placeholder="user_question, date_range" />
                  </div>
                  <div>
                    <FieldLabel>使用模型配置</FieldLabel>
                    <PixelSelect name="model_config_id" defaultValue={latestVersion?.model_config_id ?? ""}>
                      <option value="">默认模型</option>
                      {models.map((m) => (
                        <option key={m.id} value={m.id}>{m.name} ({m.model_id})</option>
                      ))}
                    </PixelSelect>
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={isSaving}
                  className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
                >
                  {isSaving ? "保存中..." : isNew ? "创建 Skill" : "保存基本信息"}
                </button>
                {!isNew && (
                  <button
                    type="button"
                    onClick={() => setShowVersionModal(true)}
                    className="border-2 border-[#1A202C] bg-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest text-gray-700 hover:bg-[#EBF4F7] transition-colors"
                  >
                    发布新版本
                  </button>
                )}
                {!isNew && isAdmin && (
                  <button
                    type="button"
                    onClick={() => setShowAiEdit((v) => !v)}
                    className="border-2 border-[#1A202C] bg-[#CCF2FF] px-5 py-2 text-[10px] font-bold uppercase tracking-widest text-[#00A3C4] hover:bg-[#00D1FF]/20 transition-colors"
                  >
                    AI 编辑
                  </button>
                )}
              </div>
            </Form>

            {/* Version history */}
            {!isNew && skill?.versions?.length > 0 && (
              <div className="mt-5 pixel-border bg-white overflow-hidden">
                <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C]">
                  <span className="text-[10px] font-bold uppercase tracking-widest">Version_History</span>
                </div>
                <div className="divide-y divide-gray-100">
                  {skill.versions.map((v: any) => (
                    <div key={v.id} className="flex items-start justify-between p-3">
                      <div>
                        <span className="text-xs font-bold text-[#1A202C] uppercase">v{v.version}</span>
                        {v.change_note && <span className="ml-2 text-[10px] text-gray-400">{v.change_note}</span>}
                        {"system_prompt" in v && (
                          <div className="mt-1 text-[9px] text-gray-400 font-mono line-clamp-1 max-w-xl">
                            {(v as any).system_prompt.slice(0, 100)}{(v as any).system_prompt.length > 100 ? "..." : ""}
                          </div>
                        )}
                      </div>
                      <span className="text-[9px] font-bold uppercase text-gray-400 flex-shrink-0 ml-4">
                        {new Date(v.created_at).toLocaleDateString("zh-CN")}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* AI Edit Panel */}
            {!isNew && showAiEdit && (
              <div className="mt-5 pixel-border bg-white overflow-hidden">
                <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
                  <span className="text-[10px] font-bold uppercase tracking-widest text-[#00D1FF]">AI_Edit_Terminal</span>
                  <button onClick={() => setShowAiEdit(false)} className="text-[10px] font-bold uppercase text-gray-400 hover:text-white">
                    [关闭]
                  </button>
                </div>
                <div className="p-5">
                  <AiEditPanel skillId={skill.id} token={token} onApplied={() => { setShowAiEdit(false); window.location.reload(); }} />
                </div>
              </div>
            )}

            {/* New version modal */}
            {showVersionModal && (
              <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
                <div className="pixel-border bg-white w-full max-w-2xl mx-4">
                  <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
                    <span className="text-[10px] font-bold uppercase tracking-widest">发布新版本</span>
                    <div className="flex space-x-1.5">
                      <div className="w-2 h-2 bg-red-400" />
                      <div className="w-2 h-2 bg-yellow-400" />
                      <div className="w-2 h-2 bg-green-400" />
                    </div>
                  </div>
                  <Form method="post" className="p-6 space-y-4">
                    <input type="hidden" name="intent" value="new_version" />
                    <div>
                      <FieldLabel>System Prompt</FieldLabel>
                      <textarea
                        name="system_prompt"
                        rows={8}
                        defaultValue={latestVersion?.system_prompt ?? ""}
                        required
                        className="w-full border-2 border-[#1A202C] bg-[#F8FAFC] px-3 py-2 text-xs font-mono font-bold focus:outline-none focus:border-[#00D1FF] resize-y"
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <FieldLabel>变量列表</FieldLabel>
                        <PixelInput name="variables" defaultValue={(latestVersion?.variables ?? []).join(", ")} />
                      </div>
                      <div>
                        <FieldLabel>模型配置</FieldLabel>
                        <PixelSelect name="model_config_id" defaultValue={latestVersion?.model_config_id ?? ""}>
                          <option value="">默认模型</option>
                          {models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                        </PixelSelect>
                      </div>
                    </div>
                    <div>
                      <FieldLabel>变更说明</FieldLabel>
                      <PixelInput
                        name="change_note"
                        value={changeNote}
                        onChange={(e) => setChangeNote(e.target.value)}
                        placeholder="本次修改了什么..."
                      />
                    </div>
                    <div className="flex gap-3 pt-2">
                      <button
                        type="submit"
                        disabled={isSaving}
                        className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
                      >
                        发布
                      </button>
                      <button
                        type="button"
                        onClick={() => setShowVersionModal(false)}
                        className="border-2 border-[#1A202C] bg-white px-5 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
                      >
                        取消
                      </button>
                    </div>
                  </Form>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
