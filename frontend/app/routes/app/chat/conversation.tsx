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

interface SandboxHistoryItem {
  session_id: number;
  target_type: string;
  target_id: number;
  target_version: number | null;
  target_name: string | null;
  tester_id: number;
  status: string;
  current_step: string;
  blocked_reason: string | null;
  detected_slots: unknown[];
  tool_review: unknown[];
  permission_snapshot: unknown[] | null;
  theoretical_combo_count: number | null;
  semantic_combo_count: number | null;
  executed_case_count: number | null;
  quality_passed: boolean | null;
  usability_passed: boolean | null;
  anti_hallucination_passed: boolean | null;
  approval_eligible: boolean | null;
  report_id: number | null;
  parent_session_id?: number | null;
  created_at: string | null;
  completed_at: string | null;
  has_report: boolean;
  report_created_at: string | null;
  report_knowledge_entry_id: number | null;
  report_hash: string | null;
}

interface SandboxReport {
  report_id: number;
  session_id: number;
  target_type: string;
  target_id: number;
  target_version: number | null;
  target_name: string | null;
  part3_evaluation: Record<string, unknown>;
  quality_passed: boolean | null;
  usability_passed: boolean | null;
  anti_hallucination_passed: boolean | null;
  approval_eligible: boolean | null;
  report_hash: string | null;
  knowledge_entry_id: number | null;
  created_at: string | null;
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusLabel(status: string): string {
  switch (status) {
    case "completed": return "已完成";
    case "running": return "执行中";
    case "cannot_test": return "不可测试";
    case "ready_to_run": return "可运行";
    case "blocked": return "已阻断";
    case "draft": return "草稿";
    default: return status;
  }
}

function stepLabel(step: string): string {
  switch (step) {
    case "input_slot_review": return "输入确认";
    case "tool_review": return "工具确认";
    case "permission_review": return "权限确认";
    case "case_generation": return "生成用例";
    case "execution": return "执行测试";
    case "evaluation": return "质量评估";
    case "done": return "已结束";
    default: return step;
  }
}

export function shouldRevalidate() {
  // 阻止 fetcher action 完成后自动 revalidation，避免闪屏和消息被吞
  return false;
}

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
    workspaceType: conv?.workspace_type ?? null,
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

function SandboxHistoryModal({
  open,
  loading,
  detailLoading,
  error,
  items,
  selectedSession,
  report,
  onClose,
  onSelectSession,
  onViewReport,
}: {
  open: boolean;
  loading: boolean;
  detailLoading: boolean;
  error: string | null;
  items: SandboxHistoryItem[];
  selectedSession: SandboxHistoryItem | null;
  report: SandboxReport | null;
  onClose: () => void;
  onSelectSession: (sessionId: number) => void;
  onViewReport: (sessionId: number) => void;
}) {
  if (!open) return null;

  return (
    <div className="absolute inset-0 z-30 bg-[#1A202C]/35 flex justify-end">
      <div className="w-full max-w-5xl h-full bg-[#F8FCFE] border-l-2 border-[#1A202C] flex">
        <div className="w-[44%] border-r-2 border-[#1A202C] flex flex-col">
          <div className="px-4 py-3 border-b-2 border-[#1A202C] bg-white flex items-center justify-between">
            <div>
              <div className="text-[9px] font-bold uppercase tracking-widest text-[#00CC99]">历史测试记录</div>
              <div className="text-[9px] text-gray-400 mt-1">清空聊天后，session 与报告仍会保留</div>
            </div>
            <button
              onClick={onClose}
              className="px-3 py-1 text-[9px] font-bold uppercase tracking-widest border-2 border-[#1A202C] bg-white text-[#1A202C] hover:bg-[#F0F4F8]"
            >
              关闭
            </button>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-3">
            {loading ? (
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">加载中...</div>
            ) : error ? (
              <div className="border-2 border-red-300 bg-red-50 px-3 py-2 text-[10px] text-red-500">{error}</div>
            ) : items.length === 0 ? (
              <div className="border-2 border-dashed border-[#00CC99] bg-white px-4 py-6 text-center">
                <div className="text-[10px] font-bold uppercase tracking-widest text-[#00CC99] mb-2">暂无历史测试</div>
                <div className="text-[10px] text-gray-500 leading-relaxed">发起沙盒测试并保存后，会在这里显示历史 session 与报告。</div>
              </div>
            ) : (
              items.map((item) => {
                const selected = item.session_id === selectedSession?.session_id;
                return (
                  <button
                    key={item.session_id}
                    onClick={() => onSelectSession(item.session_id)}
                    className={`w-full text-left border-2 px-3 py-3 transition-colors ${
                      selected ? "border-[#00CC99] bg-[#F0FFF9]" : "border-[#1A202C] bg-white hover:bg-[#F8FAFC]"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="text-[10px] font-bold text-[#1A202C]">
                          {item.target_name || `${item.target_type} #${item.target_id}`}
                        </div>
                        <div className="text-[9px] text-gray-400 mt-1">
                          v{item.target_version ?? "?"} · {formatDateTime(item.created_at)}
                        </div>
                      </div>
                      <div className={`px-2 py-0.5 text-[8px] font-bold border ${item.has_report ? "border-[#00CC99] text-[#00CC99]" : "border-gray-300 text-gray-400"}`}>
                        {item.has_report ? "有报告" : "无报告"}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 mt-3 text-[8px] font-bold uppercase tracking-widest text-gray-500">
                      <span>{statusLabel(item.status)}</span>
                      <span>•</span>
                      <span>{stepLabel(item.current_step)}</span>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>
        <div className="flex-1 flex flex-col">
          <div className="px-4 py-3 border-b-2 border-[#1A202C] bg-white">
            <div className="text-[9px] font-bold uppercase tracking-widest text-[#1A202C]">记录详情</div>
          </div>
          <div className="flex-1 overflow-y-auto p-4 space-y-4">
            {detailLoading ? (
              <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">加载详情...</div>
            ) : !selectedSession ? (
              <div className="border-2 border-dashed border-gray-300 bg-white px-4 py-6 text-[10px] text-gray-500">
                选择左侧一条历史记录后，这里会显示对应 session 与报告摘要。
              </div>
            ) : (
              <>
                <div className="border-2 border-[#1A202C] bg-white p-4 space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-[10px] font-bold uppercase tracking-widest text-[#00CC99]">Session #{selectedSession.session_id}</div>
                      <div className="text-[11px] font-bold text-[#1A202C] mt-1">
                        {selectedSession.target_name || `${selectedSession.target_type} #${selectedSession.target_id}`}
                      </div>
                    </div>
                    {selectedSession.report_id ? (
                      <button
                        onClick={() => onViewReport(selectedSession.session_id)}
                        className="px-3 py-1 text-[9px] font-bold uppercase tracking-widest border-2 border-[#00CC99] bg-[#F0FFF9] text-[#00CC99] hover:bg-[#DDFBED]"
                      >
                        查看报告
                      </button>
                    ) : null}
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[10px]">
                    <div><span className="text-gray-400">状态：</span><span className="font-bold text-[#1A202C]">{statusLabel(selectedSession.status)}</span></div>
                    <div><span className="text-gray-400">阶段：</span><span className="font-bold text-[#1A202C]">{stepLabel(selectedSession.current_step)}</span></div>
                    <div><span className="text-gray-400">创建：</span><span className="font-bold text-[#1A202C]">{formatDateTime(selectedSession.created_at)}</span></div>
                    <div><span className="text-gray-400">完成：</span><span className="font-bold text-[#1A202C]">{formatDateTime(selectedSession.completed_at)}</span></div>
                    <div><span className="text-gray-400">已执行用例：</span><span className="font-bold text-[#1A202C]">{selectedSession.executed_case_count ?? "—"}</span></div>
                    <div><span className="text-gray-400">可提审：</span><span className="font-bold text-[#1A202C]">{selectedSession.approval_eligible == null ? "—" : selectedSession.approval_eligible ? "是" : "否"}</span></div>
                  </div>
                </div>
                {report ? (
                  <div className="border-2 border-[#00CC99] bg-[#F0FFF9] p-4 space-y-3">
                    <div className="flex items-center justify-between gap-3">
                      <div className="text-[9px] font-bold uppercase tracking-widest text-[#00CC99]">报告 #{report.report_id}</div>
                      <div className="text-[9px] text-[#00CC99]/70">{formatDateTime(report.created_at)}</div>
                    </div>
                    <div className="grid grid-cols-2 gap-2 text-[10px]">
                      <div><span className="text-gray-500">知识库存证：</span><span className="font-bold text-[#1A202C]">{report.knowledge_entry_id ?? "—"}</span></div>
                      <div><span className="text-gray-500">Hash：</span><span className="font-bold text-[#1A202C]">{report.report_hash?.slice(0, 12) ?? "—"}</span></div>
                      <div><span className="text-gray-500">质量通过：</span><span className="font-bold text-[#1A202C]">{report.quality_passed == null ? "—" : report.quality_passed ? "是" : "否"}</span></div>
                      <div><span className="text-gray-500">反幻觉通过：</span><span className="font-bold text-[#1A202C]">{report.anti_hallucination_passed == null ? "—" : report.anti_hallucination_passed ? "是" : "否"}</span></div>
                    </div>
                    <pre className="text-[9px] leading-relaxed whitespace-pre-wrap break-words bg-white border border-[#BEEFD7] p-3 overflow-x-auto">
                      {JSON.stringify(report.part3_evaluation ?? {}, null, 2)}
                    </pre>
                  </div>
                ) : selectedSession.report_id ? (
                  <div className="border border-dashed border-[#00CC99] bg-white px-4 py-3 text-[10px] text-gray-500">
                    这条记录已生成报告，点击“查看报告”即可加载详情。
                  </div>
                ) : null}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ConversationPage() {
  const loaderData = useLoaderData<typeof loader>() as {
    messages: Message[];
    conversationId: number;
    workspace: { name: string; icon: string; color: string } | null;
    workspaceType: string | null;
    token: string;
    userId: number;
  };

  const { messages: initialMessages, workspace, workspaceType, token, conversationId } = loaderData;
  const fetcher = useFetcher();
  const bottomRef = useRef<HTMLDivElement>(null);

  const [draft, setDraft] = useState<DraftData | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isConverting, setIsConverting] = useState(false);
  const [optimisticMessages, setOptimisticMessages] = useState<Message[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [isFileUploading, setIsFileUploading] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyDetailLoading, setHistoryDetailLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [sandboxHistory, setSandboxHistory] = useState<SandboxHistoryItem[]>([]);
  const [selectedHistorySession, setSelectedHistorySession] = useState<SandboxHistoryItem | null>(null);
  const [selectedHistoryReport, setSelectedHistoryReport] = useState<SandboxReport | null>(null);

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

  useEffect(() => {
    if (!historyOpen || workspaceType !== "sandbox") return;
    let cancelled = false;
    const loadHistory = async () => {
      setHistoryLoading(true);
      setHistoryError(null);
      try {
        const data = await apiFetch("/api/sandbox/interactive/history?limit=20", { token });
        if (cancelled) return;
        const items = data as SandboxHistoryItem[];
        setSandboxHistory(items);
        setSelectedHistorySession((prev) =>
          prev && items.some((item) => item.session_id === prev.session_id) ? prev : items[0] ?? null
        );
      } catch (error) {
        if (!cancelled) {
          setHistoryError(error instanceof Error ? error.message : "加载历史记录失败");
          setSandboxHistory([]);
          setSelectedHistorySession(null);
        }
      } finally {
        if (!cancelled) setHistoryLoading(false);
      }
    };
    loadHistory();
    return () => {
      cancelled = true;
    };
  }, [historyOpen, token, workspaceType]);

  const handleClearConversation = async () => {
    if (isSubmitting || isLoading) {
      window.alert("当前测试进行中，请先停止生成后再清空。");
      return;
    }
    const confirmed = window.confirm("只清除当前聊天记录，历史测试 session、memo 和测试报告会保留。确认清空吗？");
    if (!confirmed) return;
    try {
      await apiFetch(`/api/conversations/${conversationId}/messages`, {
        method: "DELETE",
        token,
      });
      window.location.reload();
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "清空失败，请重试");
    }
  };

  const handleSelectHistorySession = (sessionId: number) => {
    const item = sandboxHistory.find((historyItem) => historyItem.session_id === sessionId) ?? null;
    setSelectedHistorySession(item);
    setSelectedHistoryReport(null);
  };

  const handleViewHistoryReport = async (sessionId: number) => {
    setHistoryDetailLoading(true);
    setHistoryError(null);
    try {
      const report = await apiFetch(`/api/sandbox/interactive/${sessionId}/report`, { token });
      setSelectedHistoryReport(report as SandboxReport);
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : "加载报告失败");
    } finally {
      setHistoryDetailLoading(false);
    }
  };

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
    <div className="relative flex h-full bg-[#F0F4F8]">
      <SandboxHistoryModal
        open={historyOpen}
        loading={historyLoading}
        detailLoading={historyDetailLoading}
        error={historyError}
        items={sandboxHistory}
        selectedSession={selectedHistorySession}
        report={selectedHistoryReport}
        onClose={() => setHistoryOpen(false)}
        onSelectSession={handleSelectHistorySession}
        onViewReport={handleViewHistoryReport}
      />
      {/* Left: Chat area (60%) */}
      <div
        className={`w-[60%] flex flex-col border-r-2 border-[#1A202C] relative transition-colors ${isDragOver ? "bg-[#CCF2FF]/30" : ""}`}
        onDragOver={handleChatDragOver}
        onDragLeave={handleChatDragLeave}
        onDrop={handleChatDrop}
      >
        {/* Header */}
        <div className="border-b-2 border-[#1A202C] bg-white px-4 py-2 flex-shrink-0">
          <div className="flex items-center justify-between gap-3">
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
            {workspaceType === "sandbox" && (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setHistoryOpen(true)}
                  className="px-2 py-1 text-[8px] font-bold uppercase tracking-widest border-2 border-[#00CC99] bg-[#F0FFF9] text-[#00CC99] hover:bg-[#DDFBED]"
                >
                  历史测试记录
                </button>
                <button
                  onClick={handleClearConversation}
                  disabled={isSubmitting || isLoading || messages.length === 0}
                  className="px-2 py-1 text-[8px] font-bold uppercase tracking-widest border-2 border-[#1A202C] bg-white text-[#1A202C] hover:bg-[#F0F4F8] disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  清空当前对话
                </button>
              </div>
            )}
          </div>
          {workspaceType === "sandbox" && (
            <div className="text-[8px] text-[#00CC99] mt-1">
              清空只影响当前聊天，历史 session、memo 和测试报告会保留。
            </div>
          )}
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
