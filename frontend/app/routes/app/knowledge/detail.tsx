import { useState } from "react";
import { Link, useLoaderData, useFetcher } from "react-router";
import type { Route } from "./+types/detail";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";

interface EntryDetail {
  id: number;
  title: string;
  content: string;
  content_html: string | null;
  ai_notes_html: string | null;
  category: string;
  status: string;
  source_type: string;
  source_file: string | null;
  doc_render_status: string | null;
  doc_render_error: string | null;
  doc_render_mode: string | null;
  can_retry_render: boolean;
  can_open_onlyoffice: boolean;
  lark_doc_url: string | null;
  external_edit_mode: string | null;
  source_origin_label: string | null;
  oss_key: string | null;
  file_ext: string | null;
  ai_title: string | null;
  ai_summary: string | null;
  folder_name: string | null;
  created_at: string;
}

export async function loader({ params, request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  let entry: EntryDetail | null = null;
  let error = "";
  try {
    entry = await apiFetch(`/api/knowledge/${params.id}`, { token });
  } catch (e) {
    error = e instanceof ApiError ? e.message : "加载文档失败";
  }
  return { entry, error };
}

export async function action({ params, request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;

  if (intent === "retry-render") {
    await apiFetch(`/api/knowledge/${params.id}/retry-render`, { method: "POST", token });
  }
  return null;
}

const RENDER_LABELS: Record<string, { label: string; color: string; desc: string }> = {
  pending:    { label: "转换排队中", color: "bg-yellow-100 text-yellow-700 border-yellow-400", desc: "文档正在排队等待转换，可能需要几分钟。" },
  processing: { label: "转换中",     color: "bg-yellow-100 text-yellow-700 border-yellow-400", desc: "文档正在转换为可阅读格式..." },
  ready:      { label: "转换完成",   color: "bg-green-100 text-green-700 border-green-400",   desc: "" },
  failed:     { label: "转换失败",   color: "bg-red-100 text-red-600 border-red-400",          desc: "文档转换失败，可尝试重新转换。" },
};

export default function KnowledgeDetail() {
  const { entry, error } = useLoaderData<typeof loader>() as { entry: EntryDetail | null; error: string };
  const fetcher = useFetcher();
  const [tab, setTab] = useState<"content" | "ai_notes">("content");

  if (error || !entry) {
    return (
      <div className="min-h-full bg-[#F0F4F8] p-6">
        <div className="max-w-3xl mx-auto">
          <Link to="/knowledge/my" className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">&lt; 返回列表</Link>
          <div className="mt-4 pixel-border bg-white p-8 text-center">
            <p className="text-xs font-bold uppercase text-red-500">[ERROR] {error || "文档不存在"}</p>
            <Link to="/knowledge/my" className="mt-4 inline-block text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">返回知识列表</Link>
          </div>
        </div>
      </div>
    );
  }

  const renderInfo = entry.doc_render_status ? RENDER_LABELS[entry.doc_render_status] : null;
  const hasContent = !!(entry.content_html || entry.content);
  const isLark = entry.source_type === "lark_doc";

  // 6 类展示状态判断
  const hasFallback = !!entry.content_html || !!entry.content;
  const isPendingOrProcessing = entry.doc_render_status === "pending" || entry.doc_render_status === "processing";
  const isFailed = entry.doc_render_status === "failed";
  const isReady = entry.doc_render_status === "ready";
  const isReadyButEmpty = isReady && !entry.content_html && !entry.content;

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4">
        <div className="max-w-4xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link to="/knowledge/my" className="text-[10px] font-bold uppercase text-gray-500 hover:text-[#1A202C]">&lt; 返回</Link>
            <div className="w-1.5 h-5 bg-[#00D1FF]" />
            <div>
              <h1 className="text-xs font-bold text-[#1A202C]">{entry.ai_title || entry.title}</h1>
              <div className="flex items-center gap-2 mt-0.5">
                {entry.folder_name && (
                  <span className="text-[9px] font-bold uppercase text-gray-400">{entry.folder_name}</span>
                )}
                {isLark && entry.source_origin_label && (
                  <span className="text-[9px] font-bold uppercase px-1 py-0.5 bg-blue-50 border border-blue-300 text-blue-500">
                    {entry.source_origin_label}
                  </span>
                )}
                {renderInfo && (
                  <span className={`text-[9px] font-bold uppercase px-1 py-0.5 border ${renderInfo.color}`}>
                    {renderInfo.label}
                  </span>
                )}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {isLark && entry.lark_doc_url && (
              <a
                href={entry.lark_doc_url}
                target="_blank"
                rel="noopener"
                className="text-[10px] font-bold uppercase px-3 py-1.5 border-2 border-[#3370ff] text-[#3370ff] hover:bg-[#3370ff] hover:text-white transition-colors"
              >
                在飞书中打开
              </a>
            )}
            {entry.oss_key && (
              <a
                href={`/api/knowledge/${entry.id}/download`}
                className="text-[10px] font-bold uppercase px-3 py-1.5 border-2 border-[#1A202C] bg-white text-[#1A202C] hover:bg-[#EBF4F7] transition-colors"
              >
                下载原文件
              </a>
            )}
          </div>
        </div>
      </div>

      <div className="max-w-4xl mx-auto p-6">
        {/* 状态 Banner */}
        {isPendingOrProcessing && hasFallback && (
          <div className="mb-4 border-2 border-yellow-400 bg-yellow-50 px-4 py-3 text-xs font-bold text-yellow-700">
            文档正在转换中，当前显示的是预览版本，完整格式稍后自动更新。
          </div>
        )}
        {isPendingOrProcessing && !hasFallback && (
          <div className="mb-4 border-2 border-yellow-400 bg-yellow-50 px-4 py-3 text-xs font-bold text-yellow-700">
            文档正在转换中，请稍后刷新查看。
          </div>
        )}
        {isFailed && (
          <div className="mb-4 border-2 border-red-400 bg-red-50 px-4 py-3 flex items-center justify-between">
            <div>
              <span className="text-xs font-bold text-red-600">[转换失败]</span>
              <span className="text-[10px] text-red-500 ml-2">{entry.doc_render_error || "未知错误"}</span>
            </div>
            {entry.can_retry_render && (
              <fetcher.Form method="post">
                <button name="intent" value="retry-render" className="text-[10px] font-bold uppercase px-3 py-1 border-2 border-red-400 text-red-600 hover:bg-red-100 transition-colors">
                  重试转换
                </button>
              </fetcher.Form>
            )}
          </div>
        )}
        {isReadyButEmpty && (
          <div className="mb-4 border-2 border-orange-400 bg-orange-50 px-4 py-3 text-xs font-bold text-orange-600">
            文档转换完成但正文为空 — 可能是格式不支持或内容提取失败。
            {entry.oss_key && <span> 请<a href={`/api/knowledge/${entry.id}/download`} className="underline">下载原文件</a>查看。</span>}
            {isLark && entry.lark_doc_url && <span> 或<a href={entry.lark_doc_url} target="_blank" rel="noopener" className="underline">在飞书中打开</a>。</span>}
          </div>
        )}

        {/* Tabs */}
        {entry.ai_notes_html && (
          <div className="flex border-2 border-[#1A202C] w-fit mb-4">
            <button
              onClick={() => setTab("content")}
              className={`px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest ${tab === "content" ? "bg-[#1A202C] text-white" : "bg-white text-gray-600 hover:bg-gray-100"}`}
            >
              文档正文
            </button>
            <button
              onClick={() => setTab("ai_notes")}
              className={`px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest border-l-2 border-[#1A202C] ${tab === "ai_notes" ? "bg-[#1A202C] text-white" : "bg-white text-gray-600 hover:bg-gray-100"}`}
            >
              AI 笔记
            </button>
          </div>
        )}

        {/* Content Area */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              {tab === "ai_notes" ? "AI_Notes" : "Document_Viewer"}
            </span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <div className="p-6 min-h-[400px]">
            {tab === "ai_notes" && entry.ai_notes_html ? (
              <div className="prose prose-sm max-w-none" dangerouslySetInnerHTML={{ __html: entry.ai_notes_html }} />
            ) : entry.content_html ? (
              <div className="prose prose-sm max-w-none" dangerouslySetInnerHTML={{ __html: entry.content_html }} />
            ) : entry.content ? (
              <pre className="whitespace-pre-wrap text-xs text-gray-700 font-mono leading-relaxed">{entry.content}</pre>
            ) : (
              <div className="text-center py-12">
                <p className="text-xs font-bold uppercase text-gray-400">暂无可显示内容</p>
                {entry.oss_key && (
                  <a href={`/api/knowledge/${entry.id}/download`} className="mt-2 inline-block text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">
                    下载原文件查看
                  </a>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Metadata */}
        <div className="mt-4 pixel-border bg-white p-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-[10px]">
            <div>
              <span className="font-bold uppercase text-gray-400 block">来源</span>
              <span className="font-bold text-[#1A202C] uppercase">{entry.source_type}</span>
            </div>
            <div>
              <span className="font-bold uppercase text-gray-400 block">状态</span>
              <span className="font-bold text-[#1A202C] uppercase">{entry.status}</span>
            </div>
            <div>
              <span className="font-bold uppercase text-gray-400 block">渲染模式</span>
              <span className="font-bold text-[#1A202C] uppercase">{entry.doc_render_mode || "无"}</span>
            </div>
            <div>
              <span className="font-bold uppercase text-gray-400 block">创建时间</span>
              <span className="font-bold text-[#1A202C]">{new Date(entry.created_at).toLocaleDateString("zh-CN")}</span>
            </div>
          </div>
          {entry.ai_summary && (
            <div className="mt-3 pt-3 border-t border-gray-100">
              <span className="text-[10px] font-bold uppercase text-gray-400 block mb-1">AI 摘要</span>
              <p className="text-[11px] text-gray-600">{entry.ai_summary}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
