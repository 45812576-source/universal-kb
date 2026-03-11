import { useEffect, useRef, useState } from "react";
import { data, useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/conversation";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";
import type { Message } from "~/lib/types";
import { MultimodalInput } from "~/components/chat/MultimodalInput";
import { DraftPanel } from "~/components/chat/DraftPanel";
import {
  submitRawInput,
  confirmDraftFields,
  convertDraft,
  discardDraft,
  type DraftData,
} from "~/lib/draft-api";

export async function loader({ request, params }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const [messages, convList] = await Promise.all([
    apiFetch(`/api/conversations/${params.id}/messages`, { token }),
    apiFetch("/api/conversations", { token }),
  ]);
  const conv = (convList as any[]).find((c: any) => String(c.id) === params.id);
  return {
    messages,
    conversationId: Number(params.id),
    workspace: conv?.workspace ?? null,
    token,
    userId: user.id,
  };
}

export async function action({ request, params }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const content = form.get("content") as string;

  if (!content?.trim()) {
    return data({ error: "内容不能为空" }, { status: 400 });
  }

  try {
    const result = await apiFetch(
      `/api/conversations/${params.id}/messages`,
      {
        method: "POST",
        body: JSON.stringify({ content: content.trim() }),
        token,
      }
    );
    return data(result);
  } catch (e) {
    if (e instanceof ApiError) {
      return data({ error: `请求失败: ${e.message}` }, { status: e.status });
    }
    return data({ error: "发送失败，请重试" }, { status: 500 });
  }
}

function MessageBubble({
  message,
  token,
  conversationId,
}: {
  message: Message;
  token: string;
  conversationId: number;
}) {
  const isUser = message.role === "user";
  const [reacted, setReacted] = useState<"like" | "comment" | null>(null);
  const [showCommentBox, setShowCommentBox] = useState(false);
  const [commentText, setCommentText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [savedToKb, setSavedToKb] = useState<boolean>(false);
  const [savingToKb, setSavingToKb] = useState(false);
  const [showTaskForm, setShowTaskForm] = useState(false);
  const [taskTitle, setTaskTitle] = useState("");
  const [taskPriority, setTaskPriority] = useState("neither");
  const [creatingTask, setCreatingTask] = useState(false);
  const [taskCreated, setTaskCreated] = useState(false);

  async function handleReact(type: "like" | "comment", text?: string) {
    if (message.id < 0) return; // optimistic message, skip
    setSubmitting(true);
    try {
      await apiFetch(`/api/messages/${message.id}/react`, {
        method: "POST",
        body: JSON.stringify({ reaction_type: type, comment: text }),
        token,
      });
      setReacted(type);
      setShowCommentBox(false);
      setCommentText("");
    } catch {
      // silent fail
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCreateTask(e: React.FormEvent) {
    e.preventDefault();
    if (!taskTitle.trim() || message.id < 0 || creatingTask) return;
    setCreatingTask(true);
    try {
      await apiFetch(`/api/tasks/from-message/${message.id}`, {
        method: "POST",
        body: JSON.stringify({
          title: taskTitle.trim(),
          priority: taskPriority,
        }),
        token,
      });
      setTaskCreated(true);
      setShowTaskForm(false);
    } catch {
      // silent fail
    } finally {
      setCreatingTask(false);
    }
  }

  async function handleSaveAsKnowledge() {
    if (message.id < 0 || savingToKb) return;
    setSavingToKb(true);
    try {
      await apiFetch(
        `/api/conversations/${conversationId}/messages/${message.id}/save-as-knowledge`,
        { method: "POST", token }
      );
      setSavedToKb(true);
    } catch {
      // silent fail
    } finally {
      setSavingToKb(false);
    }
  }

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-4`}>
      {!isUser && (
        <div className="w-6 h-6 bg-[#00D1FF] border-2 border-[#1A202C] text-[#1A202C] flex items-center justify-center text-[7px] font-bold mr-2 flex-shrink-0 mt-0.5 uppercase tracking-wider">
          KB
        </div>
      )}
      <div className={`max-w-[80%]`}>
        <div
          className={`px-3 py-2 ${
            isUser
              ? "bg-[#1A202C] text-white border-2 border-[#1A202C]"
              : "bg-white text-[#1A202C] border-2 border-[#1A202C]"
          }`}
        >
          <p className="text-[11px] font-bold whitespace-pre-wrap leading-relaxed">
            {message.content}
          </p>
        </div>

        {!isUser && (
          <div className="mt-1 flex items-center gap-2 flex-wrap">
            {!!message.metadata?.skill_id && (
              <>
                <div className="w-1 h-1 bg-[#00D1FF]" />
                <p className="text-[8px] text-[#00A3C4] uppercase font-bold tracking-widest">
                  via {message.metadata.skill_name ? String(message.metadata.skill_name) : `Skill #${String(message.metadata.skill_id)}`}
                </p>
              </>
            )}
            {!!message.metadata?.guide_stage && (
              <>
                <div className="w-1 h-1 bg-yellow-400" />
                <p className="text-[8px] text-yellow-600 uppercase font-bold tracking-widest">
                  {message.metadata.guide_stage === "purpose" ? "引导：确认目的" : "引导：收集信息"}
                </p>
              </>
            )}
            {/* Download button for generated files */}
            {!!message.metadata?.download_url && (
              <a
                href={String(message.metadata.download_url)}
                download={message.metadata.download_filename ? String(message.metadata.download_filename) : undefined}
                className="text-[9px] font-bold uppercase px-1.5 py-0.5 border-2 border-[#00D1FF] bg-[#00D1FF] text-[#1A202C] hover:bg-[#00A3C4] hover:border-[#00A3C4] transition-colors"
              >
                ⬇ 下载文件
              </a>
            )}
            {/* Reaction buttons — only for persisted messages */}
            {message.id > 0 && !reacted && !!message.metadata?.skill_id && (
              <>
                <button
                  onClick={() => handleReact("like")}
                  disabled={submitting}
                  title="很好"
                  className="text-[9px] font-bold uppercase px-1.5 py-0.5 border border-gray-300 bg-white text-gray-500 hover:border-[#00D1FF] hover:text-[#00A3C4] transition-colors disabled:opacity-40"
                >
                  👍 很好
                </button>
                <button
                  onClick={() => setShowCommentBox((v) => !v)}
                  disabled={submitting}
                  title="评论"
                  className="text-[9px] font-bold uppercase px-1.5 py-0.5 border border-gray-300 bg-white text-gray-500 hover:border-[#00D1FF] hover:text-[#00A3C4] transition-colors disabled:opacity-40"
                >
                  💬 评论
                </button>
              </>
            )}
            {reacted && (
              <span className="text-[8px] font-bold uppercase text-[#00A3C4]">
                {reacted === "like" ? "👍 已点赞" : "💬 已评论"}
              </span>
            )}
            {/* 沉淀为知识按钮 — 所有 assistant 消息都可用 */}
            {message.id > 0 && (
              <button
                onClick={handleSaveAsKnowledge}
                disabled={savingToKb || savedToKb}
                title="沉淀为知识"
                className="text-[9px] font-bold uppercase px-1.5 py-0.5 border border-gray-300 bg-white text-gray-500 hover:border-green-400 hover:text-green-600 transition-colors disabled:opacity-40"
              >
                {savedToKb ? "✓ 已入库" : savingToKb ? "保存中..." : "📚 入库"}
              </button>
            )}
            {/* 创建任务按钮 */}
            {message.id > 0 && (
              <button
                onClick={() => {
                  if (!taskCreated) {
                    setTaskTitle(message.content.slice(0, 50).replace(/\n/g, " "));
                    setShowTaskForm((v) => !v);
                  }
                }}
                disabled={taskCreated}
                title="创建任务"
                className="text-[9px] font-bold uppercase px-1.5 py-0.5 border border-gray-300 bg-white text-gray-500 hover:border-[#38A169] hover:text-[#38A169] transition-colors disabled:opacity-40"
              >
                {taskCreated ? "✓ 已创任务" : "📋 创任务"}
              </button>
            )}
          </div>
        )}

        {/* Comment input box */}
        {showCommentBox && (
          <div className="mt-2 border-2 border-[#1A202C] bg-white p-2">
            <textarea
              value={commentText}
              onChange={(e) => setCommentText(e.target.value)}
              rows={2}
              placeholder="写下你的评论或改进建议..."
              className="w-full text-xs font-bold text-[#1A202C] focus:outline-none resize-none"
            />
            <div className="flex gap-2 mt-1.5">
              <button
                onClick={() => handleReact("comment", commentText)}
                disabled={submitting || !commentText.trim()}
                className="text-[9px] font-bold uppercase px-3 py-1 bg-[#1A202C] text-white hover:bg-black disabled:opacity-40 transition-colors"
              >
                {submitting ? "提交中..." : "提交"}
              </button>
              <button
                onClick={() => { setShowCommentBox(false); setCommentText(""); }}
                className="text-[9px] font-bold uppercase px-3 py-1 border border-gray-300 text-gray-500 hover:bg-gray-100 transition-colors"
              >
                取消
              </button>
            </div>
          </div>
        )}

        {/* Task creation form */}
        {showTaskForm && (
          <div className="mt-2 border-2 border-[#38A169] bg-green-50 p-2">
            <form onSubmit={handleCreateTask}>
              <input
                type="text"
                value={taskTitle}
                onChange={(e) => setTaskTitle(e.target.value)}
                placeholder="任务标题"
                required
                className="w-full border border-gray-300 px-2 py-1 text-xs font-bold text-[#1A202C] focus:outline-none mb-1.5"
              />
              <div className="flex items-center gap-2">
                <select
                  value={taskPriority}
                  onChange={(e) => setTaskPriority(e.target.value)}
                  className="border border-gray-300 px-1.5 py-1 text-[10px] font-bold focus:outline-none"
                >
                  <option value="urgent_important">🔴 重要且紧急</option>
                  <option value="important">🟡 重要不紧急</option>
                  <option value="urgent">🟠 紧急不重要</option>
                  <option value="neither">⚪ 一般</option>
                </select>
                <button
                  type="submit"
                  disabled={creatingTask || !taskTitle.trim()}
                  className="text-[9px] font-bold uppercase px-3 py-1 bg-[#38A169] text-white hover:bg-green-700 disabled:opacity-40 transition-colors"
                >
                  {creatingTask ? "创建中..." : "创建"}
                </button>
                <button
                  type="button"
                  onClick={() => setShowTaskForm(false)}
                  className="text-[9px] font-bold uppercase px-2 py-1 border border-gray-300 text-gray-500 hover:bg-gray-100 transition-colors"
                >
                  取消
                </button>
              </div>
            </form>
          </div>
        )}

        {isUser && (
          <div className="mt-1 text-right">
            <span className="text-[8px] text-gray-400 font-bold uppercase tracking-wide">
              {new Date(message.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
            </span>
          </div>
        )}
      </div>
      {isUser && (
        <div className="w-6 h-6 bg-[#00CC99] border-2 border-[#1A202C] flex items-center justify-center text-[7px] font-bold ml-2 flex-shrink-0 mt-0.5 uppercase text-white">
          Me
        </div>
      )}
    </div>
  );
}

const FILE_UPLOAD_STAGES = [
  { after: 0,    label: "解析文件中..." },
  { after: 3000, label: "提取文本内容..." },
  { after: 7000, label: "生成 FOE 结构化摘要..." },
  { after: 18000, label: "校验 Input 是否充分..." },
  { after: 24000, label: "调用 Skill 生成回复..." },
];

function TypingIndicator({ isFileUpload = false }: { isFileUpload?: boolean }) {
  const [stageIdx, setStageIdx] = useState(0);

  useEffect(() => {
    if (!isFileUpload) return;
    const timers = FILE_UPLOAD_STAGES.slice(1).map((s, i) =>
      setTimeout(() => setStageIdx(i + 1), s.after)
    );
    return () => timers.forEach(clearTimeout);
  }, [isFileUpload]);

  const label = isFileUpload ? FILE_UPLOAD_STAGES[stageIdx]?.label : null;

  return (
    <div className="flex justify-start mb-4">
      <div className="w-6 h-6 bg-[#00D1FF] border-2 border-[#1A202C] text-[#1A202C] flex items-center justify-center text-[7px] font-bold mr-2 flex-shrink-0 mt-0.5 uppercase tracking-wider">
        KB
      </div>
      <div className="bg-white border-2 border-[#1A202C] px-3 py-2 flex items-center gap-2">
        <div className="flex space-x-1 items-center h-3">
          <div className="w-1 h-1 bg-[#00D1FF] animate-bounce [animation-delay:-0.3s]" />
          <div className="w-1 h-1 bg-[#00D1FF] animate-bounce [animation-delay:-0.15s]" />
          <div className="w-1 h-1 bg-[#00D1FF] animate-bounce" />
        </div>
        {label && (
          <span className="text-[9px] font-bold uppercase tracking-widest text-gray-400">
            {label}
          </span>
        )}
      </div>
    </div>
  );
}

export default function ConversationPage() {
  const loaderData = useLoaderData<typeof loader>() as {
    messages: Message[];
    conversationId: number;
    workspace: { name: string; icon: string; color: string } | null;
    token: string;
    userId: number;
  };

  const { messages: initialMessages, workspace, token, conversationId } = loaderData;
  const fetcher = useFetcher();
  const bottomRef = useRef<HTMLDivElement>(null);

  const [draft, setDraft] = useState<DraftData | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isConverting, setIsConverting] = useState(false);
  const [optimisticMessages, setOptimisticMessages] = useState<Message[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [isFileUploading, setIsFileUploading] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);

  // 全局阻止浏览器把拖入的文件在新 tab 打开（capture 捕获阶段，最早拦截）
  useEffect(() => {
    const preventOpen = (e: DragEvent) => {
      e.preventDefault(); // 不 stopPropagation，让 chat 区域的 onDrop 仍可触发
    };
    window.addEventListener("dragover", preventOpen, true);
    window.addEventListener("drop", preventOpen, true);
    return () => {
      window.removeEventListener("dragover", preventOpen, true);
      window.removeEventListener("drop", preventOpen, true);
    };
  }, []);

  const isLoading = fetcher.state !== "idle";

  const messages = [...initialMessages, ...optimisticMessages];
  if (fetcher.formData) {
    const content = fetcher.formData.get("content") as string;
    if (content) {
      messages.push({
        id: -1,
        role: "user",
        content,
        created_at: new Date().toISOString(),
      });
    }
  }
  if (fetcher.data && !fetcher.data.error) {
    if (fetcher.data.role === "assistant") {
      messages.push(fetcher.data as Message);
    }
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, isLoading]);

  const [classificationResult, setClassificationResult] = useState<any>(null);

  const handleSubmit = async (data: { text?: string; files?: File[] }) => {
    if (!data.text?.trim() && (!data.files || data.files.length === 0)) return;

    const hasDocFiles = data.files && data.files.length > 0;

    if (hasDocFiles) {
      // 乐观插入 user 消息
      const file = data.files![0];
      const optimisticUser: Message = {
        id: -2,
        role: "user",
        content: data.text ? `${data.text}\n\n[文件: ${file.name}]` : `[文件: ${file.name}]`,
        created_at: new Date().toISOString(),
        metadata: { file_upload: true, filename: file.name },
      };
      setOptimisticMessages([optimisticUser]);
      setUploadError(null);
      setIsSubmitting(true);
      setIsFileUploading(true);

      try {
        const form = new FormData();
        if (data.text) form.append("message", data.text);
        form.append("file", file);
        const resp = await fetch(`/api/conversations/${conversationId}/messages/upload`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
          body: form,
        });
        if (resp.ok) {
          const result = await resp.json();
          if (result.classification) {
            setClassificationResult(result.classification);
          }
          // 追加 assistant 回复，清除 optimistic user（loader 会带回真实历史）
          const assistantMsg: Message = {
            id: result.id,
            role: "assistant",
            content: result.content,
            created_at: new Date().toISOString(),
            metadata: result.metadata,
          };
          setOptimisticMessages([optimisticUser, assistantMsg]);
        } else {
          const err = await resp.json().catch(() => ({}));
          setUploadError(err.detail || "上传失败，请重试");
          setOptimisticMessages([]);
        }
      } catch (error) {
        console.error("File upload failed:", error);
        setUploadError("网络错误，请重试");
        setOptimisticMessages([]);
      } finally {
        setIsSubmitting(false);
        setIsFileUploading(false);
      }
      return;
    }

    // 纯文本：走原有 fetcher 发送 + 异步 draft 创建
    if (data.text?.trim()) {
      fetcher.submit({ content: data.text }, { method: "post" });
    }

    setIsSubmitting(true);
    try {
      const result = await submitRawInput(
        {
          text: data.text,
          files: data.files,
          conversationId,
        },
        token
      );
      setDraft(result.draft);
    } catch (error) {
      console.error("Failed to create draft:", error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleConfirmFields = async (
    confirmed: Record<string, any>,
    corrections?: Record<string, any>
  ) => {
    if (!draft) return;
    try {
      const updated = await confirmDraftFields(
        draft.id,
        { confirmed_fields: confirmed, corrections },
        token
      );
      setDraft(updated);
    } catch (error) {
      console.error("Failed to confirm fields:", error);
    }
  };

  const handleConvert = async () => {
    if (!draft) return;
    setIsConverting(true);
    try {
      const updated = await convertDraft(draft.id, token);
      setDraft(updated);
    } catch (error) {
      console.error("Failed to convert draft:", error);
    } finally {
      setIsConverting(false);
    }
  };

  const handleDiscard = async () => {
    if (!draft) return;
    try {
      const updated = await discardDraft(draft.id, token);
      setDraft(updated);
    } catch (error) {
      console.error("Failed to discard draft:", error);
    }
  };

  const ALLOWED_EXTS = [".txt", ".pdf", ".docx", ".pptx", ".md", ".xlsx", ".xls", ".csv",
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".mp3", ".wav", ".m4a", ".ogg", ".flac"];
  const isAllowedFile = (f: File) =>
    ALLOWED_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext)) || f.type.startsWith("image/");

  const handleChatDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  };
  const handleChatDragLeave = (e: React.DragEvent) => {
    // 只有离开整个 chat 区域时才取消高亮
    if (!e.currentTarget.contains(e.relatedTarget as Node)) {
      setIsDragOver(false);
    }
  };
  const handleChatDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    const files = Array.from(e.dataTransfer.files).filter(isAllowedFile);
    if (files.length > 0) {
      handleSubmit({ files });
    }
  };

  return (
    <div className="flex h-full bg-[#F0F4F8]">
      {/* Left: Chat area (60%) */}
      <div
        className={`w-[60%] flex flex-col border-r-2 border-[#1A202C] relative transition-colors ${isDragOver ? "bg-[#CCF2FF]/30" : ""}`}
        onDragOver={handleChatDragOver}
        onDragLeave={handleChatDragLeave}
        onDrop={handleChatDrop}
      >
        {/* Header */}
        <div className="border-b-2 border-[#1A202C] bg-white px-4 py-2 flex-shrink-0">
          <div className="flex items-center gap-2">
            {workspace ? (
              <>
                <div
                  className="w-6 h-6 flex items-center justify-center text-xs border-2 border-[#1A202C]"
                  style={{ backgroundColor: workspace.color }}
                >
                  {workspace.icon === "chat" ? "💬" :
                   workspace.icon === "data" ? "📊" :
                   workspace.icon === "search" ? "🔍" :
                   workspace.icon === "report" ? "📋" :
                   workspace.icon === "code" ? "💻" : "⚡"}
                </div>
                <span className="text-[10px] font-bold uppercase tracking-widest text-[#1A202C]">
                  {workspace.name}
                </span>
              </>
            ) : (
              <span className="text-[10px] font-bold uppercase tracking-widest text-[#1A202C]">
                AI 助手
              </span>
            )}
          </div>
        </div>

        {/* Drop overlay */}
        {isDragOver && (
          <div className="absolute inset-0 z-20 border-4 border-dashed border-[#00D1FF] bg-[#CCF2FF]/60 pointer-events-none flex items-center justify-center">
            <div className="bg-white border-2 border-[#1A202C] px-6 py-3 flex items-center gap-2">
              <span className="text-lg">📎</span>
              <span className="text-[11px] font-bold uppercase tracking-widest text-[#1A202C]">松开以上传文件</span>
            </div>
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <div className="w-8 h-8 bg-[#00D1FF] border-2 border-[#1A202C] flex items-center justify-center text-sm mb-3">
                💬
              </div>
              <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400 mb-1">
                发送消息开始对话
              </p>
              <p className="text-[9px] text-gray-400">
                输入内容后，AI 将自动提取信息并生成草稿
              </p>
            </div>
          )}
          {messages.map((msg, i) => (
            <MessageBubble
              key={msg.id === -1 ? `opt-${i}` : msg.id}
              message={msg}
              token={token}
              conversationId={conversationId}
            />
          ))}
          {(isLoading || (isSubmitting && optimisticMessages.length > 0 && optimisticMessages[optimisticMessages.length - 1]?.role === "user")) &&
            messages[messages.length - 1]?.role === "user" && (
            <TypingIndicator isFileUpload={isFileUploading} />
          )}
          {fetcher.data?.error && (
            <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-[10px] font-bold text-red-700 uppercase text-center my-2">
              [ERROR] {fetcher.data.error}
            </div>
          )}
          {uploadError && (
            <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-[10px] font-bold text-red-700 uppercase text-center my-2">
              [UPLOAD ERROR] {uploadError}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className="border-t-2 border-[#1A202C] bg-white flex-shrink-0 p-3">
          <MultimodalInput
            onSubmit={handleSubmit}
            isLoading={isSubmitting || isLoading}
            placeholder="输入消息或粘贴内容... (Ctrl+Enter 发送)"
          />
        </div>
      </div>

      {/* Right: Draft panel (40%) */}
      <div className="w-[40%] flex flex-col bg-gray-50">
        <div className="border-b-2 border-[#1A202C] bg-white px-4 py-2 flex-shrink-0">
          <span className="text-[10px] font-bold uppercase tracking-widest text-[#1A202C]">
            📋 草稿面板
          </span>
        </div>

        {/* 分类结果卡片 */}
        {classificationResult && (
          <div className="border-b-2 border-[#1A202C] bg-white p-3 flex-shrink-0">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[9px] font-bold uppercase tracking-widest text-[#00A3C4]">
                🏷 自动分类结果
              </span>
              <button
                onClick={() => setClassificationResult(null)}
                className="text-[9px] text-gray-400 hover:text-gray-600 font-bold"
              >
                ✕
              </button>
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="text-[8px] font-bold uppercase text-gray-400 w-14">板块</span>
                <span className="text-[9px] font-bold text-[#1A202C] bg-[#CCF2FF] px-1.5 py-0.5 border border-[#00D1FF]">
                  {classificationResult.taxonomy_board}
                </span>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-[8px] font-bold uppercase text-gray-400 w-14 pt-0.5">分类</span>
                <span className="text-[9px] font-bold text-[#1A202C]">
                  {classificationResult.taxonomy_path?.slice(-1)[0] || classificationResult.taxonomy_code}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[8px] font-bold uppercase text-gray-400 w-14">存储层</span>
                <span className="text-[9px] font-bold text-gray-600">
                  {classificationResult.storage_layer}
                </span>
              </div>
              {classificationResult.target_kb_ids?.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-[8px] font-bold uppercase text-gray-400 w-14 pt-0.5">知识库</span>
                  <div className="flex flex-wrap gap-1">
                    {classificationResult.target_kb_ids.map((id: string) => (
                      <span key={id} className="text-[8px] font-bold bg-gray-100 border border-gray-300 px-1 py-0.5">
                        {id}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {classificationResult.serving_skill_codes?.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-[8px] font-bold uppercase text-gray-400 w-14 pt-0.5">Skill</span>
                  <div className="flex flex-wrap gap-1">
                    {classificationResult.serving_skill_codes.map((s: string) => (
                      <span key={s} className="text-[8px] font-bold bg-yellow-50 border border-yellow-300 px-1 py-0.5 text-yellow-800">
                        {s}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              <div className="flex items-center gap-2">
                <span className="text-[8px] font-bold uppercase text-gray-400 w-14">置信度</span>
                <div className="flex items-center gap-1">
                  <div className="w-16 h-1.5 bg-gray-200 border border-gray-300">
                    <div
                      className="h-full bg-[#00D1FF]"
                      style={{ width: `${(classificationResult.confidence || 0) * 100}%` }}
                    />
                  </div>
                  <span className="text-[8px] font-bold text-gray-500">
                    {((classificationResult.confidence || 0) * 100).toFixed(0)}%
                  </span>
                </div>
              </div>
              {classificationResult.reasoning && (
                <div className="mt-1.5 text-[8px] text-gray-500 italic leading-relaxed border-t border-gray-100 pt-1.5">
                  {classificationResult.reasoning}
                </div>
              )}
            </div>
          </div>
        )}

        <div className="flex-1 overflow-hidden">
          <DraftPanel
            draft={draft}
            onConfirmFields={handleConfirmFields}
            onConvert={handleConvert}
            onDiscard={handleDiscard}
            isConverting={isConverting}
          />
        </div>
      </div>
    </div>
  );
}
