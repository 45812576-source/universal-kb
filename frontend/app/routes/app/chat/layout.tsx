import { useEffect, useState } from "react";
import { Link, Outlet, useLoaderData, useNavigate, useParams } from "react-router";
import type { Route } from "./+types/layout";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Conversation } from "~/lib/types";

const TABS_KEY = "chat_open_tabs";

export function shouldRevalidate({ formAction }: { formAction?: string }) {
  // 仅在导航到新对话时 revalidate，fetcher action 不触发
  if (formAction) return false;
  return true;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const conversations = await apiFetch("/api/conversations", { token });
  return { conversations };
}

export default function ChatLayout() {
  const { conversations } = useLoaderData<typeof loader>() as {
    conversations: Conversation[];
  };
  const params = useParams();
  const navigate = useNavigate();
  const currentId = params.id ? Number(params.id) : null;

  const [tabs, setTabs] = useState<Array<{ id: number; title: string }>>([]);
  const [hydrated, setHydrated] = useState(false);

  // Hydrate from localStorage on mount
  useEffect(() => {
    try {
      const saved = localStorage.getItem(TABS_KEY);
      if (saved) {
        setTabs(JSON.parse(saved));
      }
    } catch {}
    setHydrated(true);
  }, []);

  // Persist tabs to localStorage whenever they change
  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(TABS_KEY, JSON.stringify(tabs));
    } catch {}
  }, [tabs, hydrated]);

  // Add current conversation to tabs when navigating to one
  useEffect(() => {
    if (!hydrated || !currentId) return;
    const conv = conversations.find((c) => c.id === currentId);
    const title = conv?.title || `对话 #${currentId}`;
    setTabs((prev) => {
      if (prev.some((t) => t.id === currentId)) return prev;
      return [...prev, { id: currentId, title }];
    });
  }, [hydrated, currentId, conversations]);

  // Keep tab title in sync with conversation title (updates after first message)
  useEffect(() => {
    if (!hydrated || !currentId) return;
    const conv = conversations.find((c) => c.id === currentId);
    if (!conv?.title) return;
    setTabs((prev) =>
      prev.map((t) =>
        t.id === currentId ? { ...t, title: conv.title } : t
      )
    );
  }, [conversations, currentId, hydrated]);

  const closeTab = (tabId: number) => {
    const idx = tabs.findIndex((t) => t.id === tabId);
    const adjacent = tabs[idx + 1] || tabs[idx - 1];
    setTabs((prev) => prev.filter((t) => t.id !== tabId));
    if (tabId === currentId) {
      navigate(adjacent ? `/chat/${adjacent.id}` : "/chat");
    }
  };

  return (
    <div className="flex flex-col h-full bg-[#F0F4F8]">
      {/* Notebook-style tab bar */}
      <div className="flex-shrink-0 flex items-end border-b-2 border-[#1A202C] bg-[#EBF4F7] px-3 pt-2 overflow-x-auto min-h-[40px]">
        {tabs.map((tab) => {
          const isActive = tab.id === currentId;
          return (
            <div
              key={tab.id}
              className={`relative flex items-center gap-1.5 px-3 py-1.5 mr-1 border-2 border-b-0 flex-shrink-0 ${
                isActive
                  ? "bg-white border-[#1A202C] -mb-[2px] z-10"
                  : "bg-[#D6EDF5] border-[#8BBFD4] hover:bg-white/70"
              }`}
            >
              <Link
                to={`/chat/${tab.id}`}
                className="text-[10px] font-bold uppercase tracking-wide max-w-[140px] truncate text-[#1A202C]"
                title={tab.title}
              >
                {tab.title}
              </Link>
              <button
                onClick={(e) => {
                  e.preventDefault();
                  closeTab(tab.id);
                }}
                className="w-3.5 h-3.5 flex items-center justify-center text-[#1A202C] opacity-30 hover:opacity-80 flex-shrink-0 text-sm leading-none"
                title="关闭"
              >
                ×
              </button>
            </div>
          );
        })}

        {/* New conversation button */}
        <Link
          to="/chat"
          className="flex items-center justify-center w-7 h-7 mb-1 ml-1 border-2 border-[#1A202C] bg-[#1A202C] text-white hover:bg-black flex-shrink-0 transition-colors"
          title="新对话"
        >
          <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M12 4v16m8-8H4" />
          </svg>
        </Link>
      </div>

      {/* Main content */}
      <div className="flex-1 overflow-hidden min-w-0">
        <Outlet />
      </div>
    </div>
  );
}
