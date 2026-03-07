import { useEffect, useRef } from "react";
import { data, useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/conversation";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api.server";
import type { Message } from "~/lib/types";

export async function loader({ request, params }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const messages = await apiFetch(
    `/api/conversations/${params.id}/messages`,
    { token }
  );
  return { messages, conversationId: params.id };
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
        <div className="w-8 h-8 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center text-xs font-bold mr-2 flex-shrink-0 mt-0.5">
          AI
        </div>
      )}
      <div
        className={`max-w-[75%] rounded-2xl px-4 py-2.5 ${
          isUser
            ? "bg-blue-600 text-white rounded-br-sm"
            : "bg-white text-gray-800 shadow-sm border border-gray-100 rounded-bl-sm"
        }`}
      >
        <p className="text-sm whitespace-pre-wrap leading-relaxed">
          {message.content}
        </p>
        {!isUser && message.metadata?.skill_id && (
          <p className="text-xs text-gray-400 mt-1.5">
            Skill #{message.metadata.skill_id}
          </p>
        )}
      </div>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="flex justify-start mb-4">
      <div className="w-8 h-8 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center text-xs font-bold mr-2 flex-shrink-0">
        AI
      </div>
      <div className="bg-white rounded-2xl rounded-bl-sm px-4 py-3 shadow-sm border border-gray-100">
        <div className="flex space-x-1 items-center h-4">
          <div className="w-2 h-2 bg-gray-300 rounded-full animate-bounce [animation-delay:-0.3s]" />
          <div className="w-2 h-2 bg-gray-300 rounded-full animate-bounce [animation-delay:-0.15s]" />
          <div className="w-2 h-2 bg-gray-300 rounded-full animate-bounce" />
        </div>
      </div>
    </div>
  );
}

export default function ConversationPage() {
  const { messages: initialMessages } = useLoaderData<typeof loader>() as {
    messages: Message[];
    conversationId: string;
  };
  const fetcher = useFetcher();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const isLoading = fetcher.state !== "idle";

  // Build optimistic message list
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
    // Server returned the assistant message — it'll be in initialMessages on next load
    // but we show it optimistically
    if (fetcher.data.role === "assistant") {
      messages.push(fetcher.data as Message);
    }
  }

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, isLoading]);

  // Clear textarea after submit
  useEffect(() => {
    if (fetcher.state === "submitting" && textareaRef.current) {
      textareaRef.current.value = "";
    }
  }, [fetcher.state]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        {messages.length === 0 && (
          <div className="flex h-full items-center justify-center text-gray-400">
            <div className="text-center">
              <p className="text-3xl mb-2">✨</p>
              <p className="text-sm">发送第一条消息开始对话</p>
            </div>
          </div>
        )}
        {messages.map((msg, i) => (
          <MessageBubble key={msg.id === -1 ? `opt-${i}` : msg.id} message={msg} />
        ))}
        {isLoading && messages[messages.length - 1]?.role === "user" && (
          <TypingIndicator />
        )}
        {fetcher.data?.error && (
          <div className="text-sm text-red-500 text-center py-2">
            {fetcher.data.error}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="border-t border-gray-200 bg-white px-6 py-4">
        <fetcher.Form method="post" className="flex gap-3 items-end">
          <textarea
            ref={textareaRef}
            name="content"
            placeholder="输入消息... (Ctrl+Enter 发送)"
            rows={2}
            disabled={isLoading}
            onKeyDown={handleKeyDown}
            className="flex-1 resize-none rounded-xl border border-gray-200 px-4 py-2.5 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={isLoading}
            className="rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 transition-colors flex-shrink-0"
          >
            {isLoading ? "..." : "发送"}
          </button>
        </fetcher.Form>
        <p className="mt-1.5 text-xs text-gray-400">
          AI会根据你的问题自动匹配合适的Skill和知识
        </p>
      </div>
    </div>
  );
}
