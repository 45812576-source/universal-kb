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

function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
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
        {!isUser && message.metadata?.skill_id && (
          <div className="mt-1 flex items-center gap-1">
            <div className="w-1 h-1 bg-[#00D1FF]" />
            <p className="text-[8px] text-[#00A3C4] uppercase font-bold tracking-widest">
              via Skill #{message.metadata.skill_id}
            </p>
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

function TypingIndicator() {
  return (
    <div className="flex justify-start mb-4">
      <div className="w-6 h-6 bg-[#00D1FF] border-2 border-[#1A202C] text-[#1A202C] flex items-center justify-center text-[7px] font-bold mr-2 flex-shrink-0 uppercase tracking-wider">
        KB
      </div>
      <div className="bg-white border-2 border-[#1A202C] px-3 py-2">
        <div className="flex space-x-1 items-center h-3">
          <div className="w-1 h-1 bg-[#00D1FF] animate-bounce [animation-delay:-0.3s]" />
          <div className="w-1 h-1 bg-[#00D1FF] animate-bounce [animation-delay:-0.15s]" />
          <div className="w-1 h-1 bg-[#00D1FF] animate-bounce" />
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
    token: string;
    userId: number;
  };

  const { messages: initialMessages, workspace, token, conversationId } = loaderData;
  const fetcher = useFetcher();
  const bottomRef = useRef<HTMLDivElement>(null);

  const [draft, setDraft] = useState<DraftData | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isConverting, setIsConverting] = useState(false);

  const isLoading = fetcher.state !== "idle";

  const messages = [...initialMessages];
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

  const handleSubmit = async (data: { text?: string; files?: File[] }) => {
    if (!data.text?.trim() && (!data.files || data.files.length === 0)) return;

    // Always send as regular chat message (not blocked by draft creation)
    if (data.text?.trim()) {
      fetcher.submit({ content: data.text }, { method: "post" });
    }

    // Try to create a draft (best-effort, non-blocking)
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

  return (
    <div className="flex h-full bg-[#F0F4F8]">
      {/* Left: Chat area (60%) */}
      <div className="w-[60%] flex flex-col border-r-2 border-[#1A202C]">
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
            <MessageBubble key={msg.id === -1 ? `opt-${i}` : msg.id} message={msg} />
          ))}
          {isLoading && messages[messages.length - 1]?.role === "user" && (
            <TypingIndicator />
          )}
          {fetcher.data?.error && (
            <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-[10px] font-bold text-red-700 uppercase text-center my-2">
              [ERROR] {fetcher.data.error}
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
