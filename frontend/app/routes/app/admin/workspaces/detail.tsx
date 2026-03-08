import { useEffect, useState } from "react";
import { Link, redirect, useActionData, useLoaderData, useNavigation, useParams } from "react-router";
import type { Route } from "./+types/detail";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";

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

const ICONS = ["chat", "data", "search", "report", "code", "star"];
const ICON_EMOJI: Record<string, string> = {
  chat: "💬", data: "📊", search: "🔍", report: "📋", code: "💻", star: "⚡",
};
const COLORS = ["#00D1FF", "#00CC99", "#FF6B6B", "#FFD93D", "#6BCB77", "#845EC2", "#F9A825"];

export async function loader({ request, params }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const [skills, tools] = await Promise.all([
    apiFetch("/api/skills", { token }),
    apiFetch("/api/tools", { token }),
  ]);
  if (params.id === "new") {
    return { workspace: null, skills, tools, token, user };
  }
  const workspace = await apiFetch(`/api/workspaces/${params.id}`, { token });
  return { workspace, skills, tools, token, user };
}

export async function action({ request, params }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;

  if (intent === "save") {
    const body = {
      name: form.get("name") as string,
      description: form.get("description") as string,
      icon: form.get("icon") as string,
      color: form.get("color") as string,
      category: form.get("category") as string,
      visibility: form.get("visibility") as string,
      welcome_message: form.get("welcome_message") as string,
      system_context: (form.get("system_context") as string) || null,
      sort_order: Number(form.get("sort_order") || 0),
    };
    try {
      if (params.id === "new") {
        const result = await apiFetch("/api/workspaces", { method: "POST", body: JSON.stringify(body), token });
        return redirect(`/admin/workspaces/${result.id}`);
      } else {
        await apiFetch(`/api/workspaces/${params.id}`, { method: "PUT", body: JSON.stringify(body), token });
        return { success: "保存成功" };
      }
    } catch (e) {
      if (e instanceof ApiError) return { error: e.message };
      return { error: "保存失败" };
    }
  }

  return null;
}

// --- Skill binding tab ---
function SkillsTab({ wsId, token, allSkills }: { wsId: number; token: string; allSkills: any[] }) {
  const [boundSkillIds, setBoundSkillIds] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    try {
      const ws = await apiFetch(`/api/workspaces/${wsId}`, { token });
      setBoundSkillIds(new Set((ws.skills || []).map((s: any) => s.id)));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [wsId]);

  async function bind(skillId: number) {
    try {
      await apiFetch(`/api/workspaces/${wsId}/skills/${skillId}`, { method: "POST", token });
      setBoundSkillIds((prev) => new Set([...prev, skillId]));
    } catch (e: any) {
      setError(e.message || "绑定失败");
    }
  }

  async function unbind(skillId: number) {
    try {
      await apiFetch(`/api/workspaces/${wsId}/skills/${skillId}`, { method: "DELETE", token });
      setBoundSkillIds((prev) => { const next = new Set(prev); next.delete(skillId); return next; });
    } catch (e: any) {
      setError(e.message || "解绑失败");
    }
  }

  const published = allSkills.filter((s) => s.status === "published");

  return (
    <div className="space-y-4">
      {error && <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-700">[ERROR] {error}</div>}
      {loading ? <p className="text-xs font-bold uppercase text-gray-400">加载中...</p> : (
        <div className="space-y-2">
          {published.map((skill) => {
            const bound = boundSkillIds.has(skill.id);
            return (
              <div key={skill.id} className={`flex items-center justify-between border-2 p-3 ${bound ? "border-[#00D1FF] bg-[#CCF2FF]/10" : "border-[#1A202C] bg-white"}`}>
                <div>
                  <span className="text-xs font-bold text-[#1A202C]">{skill.name}</span>
                  {skill.description && <p className="text-[9px] text-gray-400 mt-0.5">{skill.description}</p>}
                </div>
                <button
                  onClick={() => bound ? unbind(skill.id) : bind(skill.id)}
                  className={`text-[10px] font-bold uppercase ml-4 flex-shrink-0 ${bound ? "text-red-500 hover:underline" : "text-[#00A3C4] hover:underline"}`}
                >
                  {bound ? "解绑" : "+ 绑定"}
                </button>
              </div>
            );
          })}
          {published.length === 0 && <p className="text-xs font-bold uppercase text-gray-400">暂无已发布的 Skill</p>}
        </div>
      )}
    </div>
  );
}

// --- Tool binding tab ---
function ToolsTab({ wsId, token, allTools }: { wsId: number; token: string; allTools: any[] }) {
  const [boundToolIds, setBoundToolIds] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    try {
      const ws = await apiFetch(`/api/workspaces/${wsId}`, { token });
      setBoundToolIds(new Set((ws.tools || []).map((t: any) => t.id)));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [wsId]);

  async function bind(toolId: number) {
    try {
      await apiFetch(`/api/workspaces/${wsId}/tools/${toolId}`, { method: "POST", token });
      setBoundToolIds((prev) => new Set([...prev, toolId]));
    } catch (e: any) {
      setError(e.message || "绑定失败");
    }
  }

  async function unbind(toolId: number) {
    try {
      await apiFetch(`/api/workspaces/${wsId}/tools/${toolId}`, { method: "DELETE", token });
      setBoundToolIds((prev) => { const next = new Set(prev); next.delete(toolId); return next; });
    } catch (e: any) {
      setError(e.message || "解绑失败");
    }
  }

  const activeTools = allTools.filter((t) => t.is_active);

  return (
    <div className="space-y-4">
      {error && <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-700">[ERROR] {error}</div>}
      {loading ? <p className="text-xs font-bold uppercase text-gray-400">加载中...</p> : (
        <div className="space-y-2">
          {activeTools.map((tool) => {
            const bound = boundToolIds.has(tool.id);
            return (
              <div key={tool.id} className={`flex items-center justify-between border-2 p-3 ${bound ? "border-[#00D1FF] bg-[#CCF2FF]/10" : "border-[#1A202C] bg-white"}`}>
                <div>
                  <span className="text-xs font-bold text-[#1A202C]">{tool.display_name}</span>
                  <span className="ml-2 text-[9px] font-bold uppercase text-gray-400">{tool.name}</span>
                  {tool.description && <p className="text-[9px] text-gray-400 mt-0.5">{tool.description}</p>}
                </div>
                <button
                  onClick={() => bound ? unbind(tool.id) : bind(tool.id)}
                  className={`text-[10px] font-bold uppercase ml-4 flex-shrink-0 ${bound ? "text-red-500 hover:underline" : "text-[#00A3C4] hover:underline"}`}
                >
                  {bound ? "解绑" : "+ 绑定"}
                </button>
              </div>
            );
          })}
          {activeTools.length === 0 && <p className="text-xs font-bold uppercase text-gray-400">暂无可用工具</p>}
        </div>
      )}
    </div>
  );
}

const STATUS_STYLE: Record<string, string> = {
  draft:     "bg-gray-100 text-gray-600 border-gray-400",
  reviewing: "bg-yellow-100 text-yellow-700 border-yellow-400",
  published: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]",
  archived:  "bg-red-100 text-red-600 border-red-400",
};
const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", reviewing: "审核中", published: "已发布", archived: "已归档",
};

export default function AdminWorkspaceDetail() {
  const { workspace, skills, tools, token, user } = useLoaderData<typeof loader>() as {
    workspace: any;
    skills: any[];
    tools: any[];
    token: string;
    user: any;
  };
  const actionData = useActionData<typeof action>() as any;
  const navigation = useNavigation();
  const params = useParams();
  const isNew = params.id === "new";
  const isSaving = navigation.state !== "idle";
  const isSuperAdmin = user?.role === "super_admin";
  const [activeTab, setActiveTab] = useState<"info" | "skills" | "tools">("info");

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <Link to="/admin/workspaces" className="text-[10px] font-bold uppercase text-gray-400 hover:text-[#1A202C]">
          &lt; 返回列表
        </Link>
        <span className="text-gray-300">/</span>
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">
          {isNew ? "新建工作台" : workspace?.name}
        </h1>
        {!isNew && workspace?.status && (
          <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${STATUS_STYLE[workspace.status] || ""}`}>
            {STATUS_LABEL[workspace.status] || workspace.status}
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
            {(["info", "skills", "tools"] as const).map((tab, i) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-5 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors ${i > 0 ? "border-l-2 border-[#1A202C]" : ""} ${
                  activeTab === tab ? "bg-[#1A202C] text-white" : "bg-white text-gray-500 hover:bg-[#EBF4F7]"
                }`}
              >
                {tab === "info" ? "基本信息" : tab === "skills" ? "绑定 Skill" : "绑定工具"}
              </button>
            ))}
          </div>
        )}

        {/* Skills Tab */}
        {!isNew && activeTab === "skills" && (
          <SkillsTab wsId={workspace.id} token={token} allSkills={skills} />
        )}

        {/* Tools Tab */}
        {!isNew && activeTab === "tools" && (
          <ToolsTab wsId={workspace.id} token={token} allTools={tools} />
        )}

        {/* Info Tab */}
        {(isNew || activeTab === "info") && (
          <form method="post" className="space-y-5">
            <input type="hidden" name="intent" value="save" />

            <div className="pixel-border bg-white p-5 space-y-4">
              <div className="bg-[#2D3748] text-white px-4 py-2 -mx-5 -mt-5 mb-4 border-b-2 border-[#1A202C]">
                <span className="text-[10px] font-bold uppercase tracking-widest">基本信息</span>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <FieldLabel>名称 <span className="text-[#00D1FF]">*</span></FieldLabel>
                  <PixelInput name="name" required defaultValue={workspace?.name ?? ""} placeholder="例: 销售数据分析台" />
                </div>
                <div>
                  <FieldLabel>分类</FieldLabel>
                  <PixelInput name="category" defaultValue={workspace?.category ?? "通用"} placeholder="通用 / 销售 / 运营..." />
                </div>
              </div>

              <div>
                <FieldLabel>描述</FieldLabel>
                <PixelInput name="description" defaultValue={workspace?.description ?? ""} placeholder="简述这个工作台的用途" />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <FieldLabel>图标</FieldLabel>
                  <PixelSelect name="icon" defaultValue={workspace?.icon ?? "chat"}>
                    {ICONS.map((ic) => (
                      <option key={ic} value={ic}>{ICON_EMOJI[ic]} {ic}</option>
                    ))}
                  </PixelSelect>
                </div>
                <div>
                  <FieldLabel>颜色</FieldLabel>
                  <PixelSelect name="color" defaultValue={workspace?.color ?? "#00D1FF"}>
                    {COLORS.map((c) => (
                      <option key={c} value={c} style={{ backgroundColor: c }}>{c}</option>
                    ))}
                  </PixelSelect>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <FieldLabel>可见性</FieldLabel>
                  <PixelSelect name="visibility" defaultValue={workspace?.visibility ?? "all"}>
                    <option value="all">全员可见</option>
                    <option value="department">仅部门内</option>
                  </PixelSelect>
                </div>
                <div>
                  <FieldLabel>排序权重</FieldLabel>
                  <PixelInput name="sort_order" type="number" defaultValue={workspace?.sort_order ?? 0} />
                </div>
              </div>
            </div>

            <div className="pixel-border bg-white p-5 space-y-4">
              <div className="bg-[#2D3748] text-white px-4 py-2 -mx-5 -mt-5 mb-4 border-b-2 border-[#1A202C]">
                <span className="text-[10px] font-bold uppercase tracking-widest">对话配置</span>
              </div>
              <div>
                <FieldLabel>欢迎语</FieldLabel>
                <PixelInput
                  name="welcome_message"
                  defaultValue={workspace?.welcome_message ?? "你好，有什么可以帮你的？"}
                />
              </div>
              {isSuperAdmin && (
                <div>
                  <FieldLabel>系统附加 Prompt（仅超管可见）</FieldLabel>
                  <textarea
                    name="system_context"
                    rows={5}
                    defaultValue={workspace?.system_context ?? ""}
                    className="w-full border-2 border-[#1A202C] bg-[#F8FAFC] px-3 py-2.5 text-xs font-mono font-bold focus:outline-none focus:border-[#00D1FF] resize-y"
                    placeholder="附加给所有对话的额外系统提示..."
                  />
                </div>
              )}
            </div>

            <div className="flex gap-3">
              <button
                type="submit"
                disabled={isSaving}
                className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
              >
                {isSaving ? "保存中..." : isNew ? "创建工作台" : "保存"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
