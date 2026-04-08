import { useState, useEffect, useRef, useCallback } from "react";
import { data, redirect, useActionData, useLoaderData, useNavigation, useSearchParams, Link } from "react-router";
import { Form } from "react-router";
import type { Route } from "./+types/my";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";
import type { KnowledgeEntry } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const url = new URL(request.url);
  const source_type = url.searchParams.get("source_type") || "";
  const status = url.searchParams.get("status") || "";
  const params = new URLSearchParams();
  if (source_type) params.set("source_type", source_type);
  if (status) params.set("status", status);

  // 容错：entries 加载失败不白屏
  let entries: KnowledgeEntry[] = [];
  let loadError = "";
  try {
    entries = await apiFetch(`/api/knowledge${params.toString() ? "?" + params : ""}`, { token });
  } catch (e) {
    loadError = e instanceof ApiError ? e.message : "加载知识列表失败";
  }

  return { entries, source_type, status, loadError };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const mode = form.get("mode") as string;

  const tagsFromField = (field: string) =>
    (form.get(field) as string || "")
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

  try {
    if (mode === "text") {
      const result = await apiFetch("/api/knowledge", {
        method: "POST",
        body: JSON.stringify({
          title: form.get("title") as string,
          content: form.get("content") as string,
          category: form.get("category") as string,
          industry_tags: tagsFromField("industry_tags"),
          platform_tags: tagsFromField("platform_tags"),
          topic_tags: tagsFromField("topic_tags"),
        }),
        token,
      });
      return redirect(`/knowledge/my?submitted=${result.id}`);
    } else {
      const uploadForm = new FormData();
      uploadForm.set("title", form.get("title") as string);
      uploadForm.set("category", form.get("category") as string);
      uploadForm.set("industry_tags", JSON.stringify(tagsFromField("industry_tags")));
      uploadForm.set("platform_tags", JSON.stringify(tagsFromField("platform_tags")));
      uploadForm.set("topic_tags", JSON.stringify(tagsFromField("topic_tags")));
      const file = form.get("file") as File;
      uploadForm.set("file", file);
      const result = await apiFetch("/api/knowledge/upload", {
        method: "POST",
        body: uploadForm,
        token,
      });
      return redirect(`/knowledge/my?submitted=${result.id}`);
    }
  } catch (e) {
    if (e instanceof ApiError) return data({ error: e.message }, { status: e.status });
    return data({ error: "提交失败，请重试" }, { status: 500 });
  }
}

const STATUS_INFO: Record<string, { label: string; color: string }> = {
  pending:  { label: "待审核", color: "bg-yellow-100 text-yellow-800 border-yellow-400" },
  approved: { label: "已通过", color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
  rejected: { label: "已拒绝", color: "bg-red-100 text-red-700 border-red-400" },
  archived: { label: "已归档", color: "bg-gray-100 text-gray-500 border-gray-400" },
};

const SOURCE_LABELS: Record<string, string> = {
  manual: "手动录入",
  upload: "文件上传",
  auto_collected: "自动采集",
  chat_output: "对话产出",
  chat_upload: "对话上传",
  lark_doc: "飞书导入",
};

const CATEGORY_LABELS: Record<string, string> = {
  experience: "经验总结",
  methodology: "方法论",
  case_study: "案例",
  data: "数据资产",
  template: "模板",
  external: "外部资料",
};

const CATEGORIES = [
  { value: "experience", label: "经验总结" },
  { value: "methodology", label: "方法论" },
  { value: "case_study", label: "案例" },
  { value: "data", label: "数据资产" },
  { value: "template", label: "模板" },
  { value: "external", label: "外部资料" },
];

function PixelInput({ className, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${className || ""}`}
    />
  );
}

function FieldLabel({ children, required }: { children: React.ReactNode; required?: boolean }) {
  return (
    <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
      {children}{required && <span className="text-[#00D1FF] ml-1">*</span>}
    </label>
  );
}

function CreateForm({ onCancel }: { onCancel: () => void }) {
  const actionData = useActionData<typeof action>() as any;
  const navigation = useNavigation();
  const [mode, setMode] = useState<"text" | "file">("text");
  const isSubmitting = navigation.state !== "idle";

  return (
    <div className="mb-6 pixel-border bg-white overflow-hidden">
      <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
        <span className="text-[10px] font-bold uppercase tracking-widest">New_Knowledge_Entry</span>
        <div className="flex items-center gap-3">
          <div className="flex space-x-1.5">
            <div className="w-2 h-2 bg-red-400" />
            <div className="w-2 h-2 bg-yellow-400" />
            <div className="w-2 h-2 bg-green-400" />
          </div>
          <button
            type="button"
            onClick={onCancel}
            className="text-gray-400 hover:text-white text-xs font-bold"
          >
            [收起]
          </button>
        </div>
      </div>

      <div className="p-5">
        {actionData?.error && (
          <div className="mb-4 border-2 border-red-400 bg-red-50 px-4 py-3 text-xs font-bold text-red-700 uppercase">
            [ERROR] {actionData.error}
          </div>
        )}

        {/* Mode toggle */}
        <div className="flex border-2 border-[#1A202C] mb-5 w-fit">
          <button
            type="button"
            onClick={() => setMode("text")}
            className={`px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest transition-colors ${
              mode === "text" ? "bg-[#1A202C] text-white" : "bg-white text-gray-600 hover:bg-gray-100"
            }`}
          >
            文字录入
          </button>
          <button
            type="button"
            onClick={() => setMode("file")}
            className={`px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest border-l-2 border-[#1A202C] transition-colors ${
              mode === "file" ? "bg-[#1A202C] text-white" : "bg-white text-gray-600 hover:bg-gray-100"
            }`}
          >
            文件上传
          </button>
        </div>

        <Form
          method="post"
          encType={mode === "file" ? "multipart/form-data" : undefined}
          className="space-y-4"
        >
          <input type="hidden" name="mode" value={mode} />

          <div className="grid grid-cols-2 gap-4">
            <div>
              <FieldLabel required>标题</FieldLabel>
              <PixelInput name="title" required placeholder="例: 618大促投放ROI提升方法论" />
            </div>
            <div>
              <FieldLabel>分类</FieldLabel>
              <select
                name="category"
                className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
              >
                {CATEGORIES.map((c) => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </select>
            </div>
          </div>

          {mode === "text" ? (
            <div>
              <FieldLabel required>内容</FieldLabel>
              <textarea
                name="content"
                required
                rows={6}
                className="w-full border-2 border-[#1A202C] bg-white px-3 py-2.5 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-y"
                placeholder="请详细描述你的经验、方法或案例..."
              />
            </div>
          ) : (
            <div>
              <FieldLabel required>文件</FieldLabel>
              <div className="border-2 border-dashed border-[#1A202C] px-6 py-6 text-center bg-white">
                <p className="text-[10px] font-bold uppercase text-gray-400 mb-2">
                  支持 PDF / DOCX / PPTX / MD / TXT
                </p>
                <input
                  type="file"
                  name="file"
                  required
                  accept=".pdf,.docx,.pptx,.md,.txt"
                  className="block w-full text-xs font-bold text-gray-500 file:mr-3 file:py-1.5 file:px-4 file:border-2 file:border-[#1A202C] file:bg-[#CCF2FF] file:text-xs file:font-bold file:uppercase cursor-pointer"
                />
              </div>
            </div>
          )}

          <div className="border-2 border-[#1A202C] bg-[#EBF4F7] p-3 space-y-2">
            <p className="text-[9px] font-bold text-[#00A3C4] uppercase tracking-widest">
              — 标签（逗号分隔，可选）
            </p>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <FieldLabel>行业</FieldLabel>
                <PixelInput name="industry_tags" placeholder="电商, 快消" />
              </div>
              <div>
                <FieldLabel>平台</FieldLabel>
                <PixelInput name="platform_tags" placeholder="天猫, 抖音" />
              </div>
              <div>
                <FieldLabel>主题</FieldLabel>
                <PixelInput name="topic_tags" placeholder="ROI优化, 数据分析" />
              </div>
            </div>
          </div>

          <div className="flex gap-3">
            <button
              type="submit"
              disabled={isSubmitting}
              className="bg-[#1A202C] text-white px-6 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
            >
              {isSubmitting ? "提交中..." : "> 提交审核"}
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="border-2 border-[#1A202C] bg-white px-6 py-2 text-[10px] font-bold uppercase tracking-widest text-gray-600 hover:bg-gray-100 transition-colors"
            >
              取消
            </button>
          </div>
        </Form>
      </div>
    </div>
  );
}

interface Grant {
  id: number;
  user_id: number;
  user_name: string | null;
  granted_by: number;
  created_at: string | null;
}

interface SuggestedUser {
  id: number;
  display_name: string;
  username: string;
}

function CollabPanel({ entryId, onClose }: { entryId: number; onClose: () => void }) {
  const [grants, setGrants] = useState<Grant[]>([]);
  const [search, setSearch] = useState("");
  const [suggestions, setSuggestions] = useState<SuggestedUser[]>([]);
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  const loadGrants = useCallback(async () => {
    const res = await fetch(`/api/knowledge/${entryId}/edit-grants`);
    if (res.ok) setGrants(await res.json());
  }, [entryId]);

  useEffect(() => { loadGrants(); }, [loadGrants]);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) onClose();
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [onClose]);

  useEffect(() => {
    if (!search.trim()) { setSuggestions([]); return; }
    const t = setTimeout(async () => {
      const exclude = grants.map((g) => g.user_id).join(",");
      const res = await fetch(`/api/admin/users/suggested?q=${encodeURIComponent(search)}&exclude=${exclude}`);
      if (res.ok) setSuggestions(await res.json());
    }, 300);
    return () => clearTimeout(t);
  }, [search, grants]);

  const handleGrant = async (uid: number) => {
    setLoading(true);
    const res = await fetch(`/api/knowledge/${entryId}/edit-grants`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_ids: [uid] }),
    });
    if (res.ok) { setGrants(await res.json()); setSearch(""); setSuggestions([]); }
    setLoading(false);
  };

  const handleRevoke = async (uid: number) => {
    await fetch(`/api/knowledge/${entryId}/edit-grants/${uid}`, { method: "DELETE" });
    loadGrants();
  };

  return (
    <div ref={panelRef} className="absolute right-0 top-full mt-1 z-50 w-72 border-2 border-[#1A202C] bg-white shadow-lg">
      <div className="bg-[#2D3748] text-white px-3 py-2 flex items-center justify-between">
        <span className="text-[10px] font-bold uppercase tracking-widest">协作者管理</span>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xs font-bold">[X]</button>
      </div>
      <div className="p-3 space-y-3">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索用户..."
          className="w-full border-2 border-[#1A202C] bg-white px-2 py-1.5 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
        />

        {suggestions.length > 0 && (
          <div className="border border-gray-200 divide-y divide-gray-100 max-h-32 overflow-y-auto">
            {suggestions.map((u) => (
              <div key={u.id} className="flex items-center justify-between px-2 py-1.5">
                <span className="text-xs font-bold text-[#1A202C] truncate">{u.display_name}</span>
                <button
                  onClick={() => handleGrant(u.id)}
                  disabled={loading}
                  className="text-[9px] font-bold uppercase text-[#00A3C4] hover:text-[#00D1FF] disabled:opacity-50"
                >
                  + 授权编辑
                </button>
              </div>
            ))}
          </div>
        )}

        {grants.length > 0 && (
          <div>
            <p className="text-[9px] font-bold uppercase text-gray-400 mb-1">已授权:</p>
            <div className="border border-gray-200 divide-y divide-gray-100 max-h-40 overflow-y-auto">
              {grants.map((g) => (
                <div key={g.id} className="flex items-center justify-between px-2 py-1.5">
                  <div>
                    <span className="text-xs font-bold text-[#1A202C]">{g.user_name || `用户#${g.user_id}`}</span>
                    <span className="text-[9px] text-gray-400 ml-1.5">可编辑</span>
                  </div>
                  <button
                    onClick={() => handleRevoke(g.user_id)}
                    className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700"
                  >
                    撤销
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {grants.length === 0 && !search && (
          <p className="text-[10px] font-bold text-gray-400 text-center py-2">暂无协作者</p>
        )}
      </div>
    </div>
  );
}

const RENDER_STATUS_INFO: Record<string, { label: string; color: string }> = {
  pending:    { label: "转换中", color: "bg-yellow-100 text-yellow-700 border-yellow-400" },
  processing: { label: "转换中", color: "bg-yellow-100 text-yellow-700 border-yellow-400" },
  ready:      { label: "可查看", color: "bg-green-100 text-green-700 border-green-400" },
  failed:     { label: "转换失败", color: "bg-red-100 text-red-600 border-red-400" },
};

export default function MyKnowledge() {
  const { entries, source_type, status, loadError } = useLoaderData<typeof loader>() as {
    entries: KnowledgeEntry[];
    source_type: string;
    status: string;
    loadError: string;
  };
  const [searchParams] = useSearchParams();
  const justSubmitted = searchParams.get("submitted");
  const [showForm, setShowForm] = useState(false);
  const [collabEntryId, setCollabEntryId] = useState<number | null>(null);
  const [copiedId, setCopiedId] = useState<number | null>(null);

  const handleCopyLink = (entryId: number) => {
    const url = `${window.location.origin}/knowledge/${entryId}`;
    navigator.clipboard.writeText(url).then(() => {
      setCopiedId(entryId);
      setTimeout(() => setCopiedId(null), 2000);
    });
  };

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">我的知识</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">知识条目管理与录入</p>
          </div>
        </div>
        {!showForm && (
          <button
            onClick={() => setShowForm(true)}
            className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
          >
            + 录入新知识
          </button>
        )}
      </div>

      <div className="p-6 max-w-5xl">
        {loadError && (
          <div className="mb-4 border-2 border-red-400 bg-red-50 px-4 py-3 text-xs font-bold text-red-600 uppercase">
            [ERROR] {loadError}
          </div>
        )}
        {justSubmitted && (
          <div className="mb-4 border-2 border-[#00D1FF] bg-[#CCF2FF]/30 px-4 py-3 text-xs font-bold text-[#00A3C4] uppercase">
            [OK] 提交成功！已进入审核队列，管理员审核后将自动入库。
          </div>
        )}

        {/* Inline create form */}
        {showForm && <CreateForm onCancel={() => setShowForm(false)} />}

        {/* Filters */}
        <form method="get" className="flex gap-3 mb-4 flex-wrap">
          <select
            name="source_type"
            defaultValue={source_type}
            className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-[10px] font-bold uppercase focus:outline-none focus:border-[#00D1FF]"
          >
            <option value="">全部来源</option>
            <option value="manual">手动录入</option>
            <option value="upload">文件上传</option>
            <option value="chat_output">对话产出</option>
            <option value="chat_upload">对话上传</option>
            <option value="auto_collected">自动采集</option>
            <option value="lark_doc">飞书导入</option>
          </select>
          <select
            name="status"
            defaultValue={status}
            className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-[10px] font-bold uppercase focus:outline-none focus:border-[#00D1FF]"
          >
            <option value="">全部状态</option>
            <option value="pending">待审核</option>
            <option value="approved">已通过</option>
            <option value="rejected">已拒绝</option>
          </select>
          <button
            type="submit"
            className="border-2 border-[#1A202C] bg-white px-4 py-1.5 text-[10px] font-bold uppercase hover:bg-[#EBF4F7] transition-colors"
          >
            筛选
          </button>
          {(source_type || status) && (
            <a
              href="/knowledge/my"
              className="border-2 border-gray-400 bg-white px-4 py-1.5 text-[10px] font-bold uppercase text-gray-500 hover:bg-gray-100 transition-colors"
            >
              清除
            </a>
          )}
        </form>

        {/* List */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Knowledge_List ({entries.length})</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <table className="w-full text-left">
            <thead>
              <tr className="border-b-2 border-[#1A202C] bg-[#F0F4F8]">
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">标题</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">分类</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">来源</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">标签</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">提交时间</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {entries.map((e) => {
                const si = STATUS_INFO[e.status] || { label: e.status, color: "bg-gray-100 text-gray-500 border-gray-400" };
                const allTags = [...e.industry_tags, ...e.platform_tags, ...e.topic_tags].slice(0, 4);
                return (
                  <tr key={e.id} className="hover:bg-[#F0F4F8] transition-colors">
                    <td className="py-3 px-4">
                      <Link to={`/knowledge/${e.id}`} className="text-xs font-bold text-[#1A202C] hover:text-[#00A3C4] truncate max-w-xs block">{e.ai_title || e.title}</Link>
                      <div className="text-[10px] text-gray-400 mt-0.5 truncate max-w-xs">{e.ai_summary || e.content}</div>
                      <div className="flex items-center gap-1.5 mt-1">
                        {e.doc_render_status && (() => {
                          const ri = RENDER_STATUS_INFO[e.doc_render_status];
                          return ri ? (
                            <span className={`inline-block border px-1 py-0.5 text-[8px] font-bold uppercase ${ri.color}`} title={e.doc_render_error || ""}>
                              {ri.label}
                            </span>
                          ) : null;
                        })()}
                        {e.source_type === "lark_doc" && e.lark_doc_url && (
                          <a href={e.lark_doc_url} target="_blank" rel="noopener" className="text-[8px] font-bold text-[#3370ff] hover:underline uppercase">
                            飞书原文
                          </a>
                        )}
                        {e.doc_render_status === "failed" && e.can_retry_render && (
                          <button
                            onClick={() => fetch(`/api/knowledge/${e.id}/retry-render`, { method: "POST" })}
                            className="text-[8px] font-bold text-orange-500 hover:underline uppercase"
                          >
                            重试转换
                          </button>
                        )}
                      </div>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold uppercase text-gray-500">
                      {CATEGORY_LABELS[e.category] || e.category}
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-[#EBF4F7] border border-gray-300 text-gray-600">
                        {SOURCE_LABELS[e.source_type] || e.source_type}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${si.color}`}>
                        {si.label}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex flex-wrap gap-1">
                        {allTags.map((tag) => (
                          <span key={tag} className="px-1.5 py-0.5 text-[9px] font-bold uppercase bg-[#EBF4F7] border border-[#1A202C] text-gray-600">
                            {tag}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold text-gray-400 uppercase">
                      {new Date(e.created_at).toLocaleDateString("zh-CN")}
                    </td>
                    <td className="py-3 px-4 relative">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => handleCopyLink(e.id)}
                          className="text-[9px] font-bold uppercase px-2 py-1 border-2 border-[#00D1FF] bg-[#CCF2FF] text-[#00A3C4] hover:bg-[#00D1FF] hover:text-white transition-colors"
                        >
                          {copiedId === e.id ? "已复制" : "分享链接"}
                        </button>
                        <button
                          onClick={() => setCollabEntryId(collabEntryId === e.id ? null : e.id)}
                          className="text-[9px] font-bold uppercase px-2 py-1 border-2 border-[#1A202C] bg-white text-[#1A202C] hover:bg-[#EBF4F7] transition-colors"
                        >
                          协作者
                        </button>
                      </div>
                      {collabEntryId === e.id && (
                        <CollabPanel entryId={e.id} onClose={() => setCollabEntryId(null)} />
                      )}
                    </td>
                  </tr>
                );
              })}
              {entries.length === 0 && (
                <tr>
                  <td colSpan={7} className="py-12 text-center text-xs font-bold uppercase text-gray-400">
                    暂无数据 —{" "}
                    <button
                      onClick={() => setShowForm(true)}
                      className="text-[#00A3C4] hover:underline"
                    >
                      去录入第一条
                    </button>
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
