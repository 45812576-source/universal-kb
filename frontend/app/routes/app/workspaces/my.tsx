import { useState } from "react";
import { useLoaderData, useRevalidator } from "react-router";
import type { Route } from "./+types/my";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";

const ICONS = ["chat", "data", "search", "report", "code", "star"];
const ICON_EMOJI: Record<string, string> = {
  chat: "💬", data: "📊", search: "🔍", report: "📋", code: "💻", star: "⚡",
};
const COLORS = ["#00D1FF", "#00CC99", "#FF6B6B", "#FFD93D", "#6BCB77", "#845EC2", "#F9A825"];
const MAX_DRAFT = 3;

const STATUS_STYLE: Record<string, string> = {
  draft:     "bg-gray-100 text-gray-600 border-gray-400",
  reviewing: "bg-yellow-100 text-yellow-700 border-yellow-400",
  published: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]",
};
const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", reviewing: "审核中", published: "已发布",
};

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const all = await apiFetch("/api/workspaces", { token });
  const mine = (all as any[]).filter(
    (w: any) => w.created_by === user.id && ["draft", "reviewing", "published"].includes(w.status)
  );
  return { workspaces: mine, token, user };
}

export default function MyWorkspaces() {
  const { workspaces: initial, token, user } = useLoaderData<typeof loader>() as {
    workspaces: any[];
    token: string;
    user: any;
  };
  const revalidator = useRevalidator();
  const [workspaces, setWorkspaces] = useState(initial);
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    name: "", description: "", icon: "chat", color: "#00D1FF", category: "通用",
    welcome_message: "你好，有什么可以帮你的？",
  });

  const draftCount = workspaces.filter((w) => w.status === "draft").length;

  async function create() {
    if (!form.name.trim()) { setError("请填写名称"); return; }
    setCreating(true);
    setError("");
    try {
      const result = await apiFetch("/api/workspaces", {
        method: "POST",
        body: JSON.stringify(form),
        token,
      });
      setWorkspaces((prev) => [...prev, result]);
      setShowCreate(false);
      setForm({ name: "", description: "", icon: "chat", color: "#00D1FF", category: "通用", welcome_message: "你好，有什么可以帮你的？" });
    } catch (e: any) {
      setError(e.message || "创建失败");
    } finally {
      setCreating(false);
    }
  }

  async function submit(wsId: number) {
    setError("");
    try {
      await apiFetch(`/api/workspaces/${wsId}/submit`, { method: "PATCH", token });
      setWorkspaces((prev) =>
        prev.map((w) => w.id === wsId ? { ...w, status: "reviewing" } : w)
      );
    } catch (e: any) {
      setError(e.message || "提交失败");
    }
  }

  async function del(wsId: number) {
    if (!confirm("确定删除该草稿工作台？")) return;
    setError("");
    try {
      await apiFetch(`/api/workspaces/${wsId}`, { method: "DELETE", token });
      setWorkspaces((prev) => prev.filter((w) => w.id !== wsId));
    } catch (e: any) {
      setError(e.message || "删除失败");
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">我的工作台</h1>
          <span className="text-[9px] font-bold uppercase text-gray-400">
            草稿 {draftCount}/{MAX_DRAFT}
          </span>
        </div>
        {draftCount < MAX_DRAFT && (
          <button
            onClick={() => setShowCreate(true)}
            className="bg-[#1A202C] text-white px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
          >
            + 新建
          </button>
        )}
      </div>

      <div className="p-6 max-w-3xl space-y-4">
        {error && (
          <div className="border-2 border-red-400 bg-red-50 px-4 py-2 text-xs font-bold text-red-700 uppercase">
            [ERROR] {error}
          </div>
        )}

        {draftCount >= MAX_DRAFT && (
          <div className="border-2 border-yellow-400 bg-yellow-50 px-4 py-2 text-xs font-bold text-yellow-700 uppercase">
            草稿名额已满（{MAX_DRAFT}/{MAX_DRAFT}）。提交审核通过后可释放名额。
          </div>
        )}

        {/* Create form */}
        {showCreate && (
          <div className="pixel-border bg-white overflow-hidden">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-widest">新建工作台</span>
              <button onClick={() => setShowCreate(false)} className="text-[10px] font-bold uppercase text-gray-400 hover:text-white">[关闭]</button>
            </div>
            <div className="p-5 space-y-3">
              <div>
                <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1">名称 *</label>
                <input
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  placeholder="工作台名称"
                  className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
                />
              </div>
              <div>
                <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1">描述</label>
                <input
                  value={form.description}
                  onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                  placeholder="简述用途"
                  className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
                />
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1">图标</label>
                  <select
                    value={form.icon}
                    onChange={(e) => setForm((f) => ({ ...f, icon: e.target.value }))}
                    className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
                  >
                    {ICONS.map((ic) => <option key={ic} value={ic}>{ICON_EMOJI[ic]} {ic}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1">颜色</label>
                  <select
                    value={form.color}
                    onChange={(e) => setForm((f) => ({ ...f, color: e.target.value }))}
                    className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
                  >
                    {COLORS.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1">分类</label>
                  <input
                    value={form.category}
                    onChange={(e) => setForm((f) => ({ ...f, category: e.target.value }))}
                    placeholder="通用"
                    className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
                  />
                </div>
              </div>
              <div>
                <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1">欢迎语</label>
                <input
                  value={form.welcome_message}
                  onChange={(e) => setForm((f) => ({ ...f, welcome_message: e.target.value }))}
                  className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
                />
              </div>
              <div className="flex gap-3 pt-1">
                <button
                  onClick={create}
                  disabled={creating}
                  className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
                >
                  {creating ? "创建中..." : "创建"}
                </button>
                <button
                  onClick={() => setShowCreate(false)}
                  className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
                >
                  取消
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Workspace list */}
        {workspaces.length === 0 && !showCreate && (
          <div className="text-center py-16">
            <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">
              暂无自建工作台。点击"新建"创建属于你的专属智能体。
            </p>
          </div>
        )}

        {workspaces.map((ws) => (
          <div key={ws.id} className="border-2 border-[#1A202C] bg-white p-4">
            <div className="flex items-start justify-between">
              <div className="flex items-start gap-3">
                <div
                  className="w-8 h-8 flex items-center justify-center text-sm border-2 border-[#1A202C] flex-shrink-0"
                  style={{ backgroundColor: ws.color }}
                >
                  {ICON_EMOJI[ws.icon] ?? "⚡"}
                </div>
                <div>
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-xs font-bold text-[#1A202C]">{ws.name}</span>
                    <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${STATUS_STYLE[ws.status] || ""}`}>
                      {STATUS_LABEL[ws.status] || ws.status}
                    </span>
                  </div>
                  {ws.description && (
                    <p className="text-[10px] text-gray-500">{ws.description}</p>
                  )}
                  <p className="text-[9px] text-gray-400 mt-0.5 uppercase font-bold">{ws.category}</p>
                </div>
              </div>
              <div className="flex items-center gap-3 flex-shrink-0 ml-4">
                {ws.status === "draft" && (
                  <>
                    <button
                      onClick={() => submit(ws.id)}
                      className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                    >
                      提交审核
                    </button>
                    <button
                      onClick={() => del(ws.id)}
                      className="text-[10px] font-bold uppercase text-red-500 hover:underline"
                    >
                      删除
                    </button>
                  </>
                )}
                {ws.status === "reviewing" && (
                  <span className="text-[10px] font-bold uppercase text-yellow-600">审核中...</span>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
