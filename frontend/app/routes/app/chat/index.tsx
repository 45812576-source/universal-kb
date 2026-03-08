import { Form, useLoaderData, useNavigation } from "react-router";
import { redirect } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Workspace } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  let workspaces: Workspace[] = [];
  try {
    workspaces = await apiFetch("/api/workspaces", { token });
  } catch {}
  return { workspaces, user };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const formData = await request.formData();
  const workspaceId = formData.get("workspace_id");
  const body = workspaceId ? { workspace_id: Number(workspaceId) } : {};
  const data = await apiFetch("/api/conversations", {
    method: "POST",
    body: JSON.stringify(body),
    token,
  });
  return redirect(`/chat/${data.id}`);
}

function WorkspaceCard({ workspace }: { workspace: Workspace }) {
  return (
    <Form method="post">
      <input type="hidden" name="workspace_id" value={workspace.id} />
      <button
        type="submit"
        className="w-full text-left border-2 border-[#1A202C] bg-white p-4 hover:border-[#00D1FF] hover:bg-[#EBF4F7] transition-colors group"
      >
        <div
          className="w-8 h-8 flex items-center justify-center text-lg mb-3 border-2 border-[#1A202C]"
          style={{ backgroundColor: workspace.color }}
        >
          {workspace.icon === "chat" ? "💬" :
           workspace.icon === "data" ? "📊" :
           workspace.icon === "search" ? "🔍" :
           workspace.icon === "report" ? "📋" :
           workspace.icon === "code" ? "💻" :
           "⚡"}
        </div>
        <div className="text-[10px] font-bold uppercase tracking-wide text-[#1A202C] group-hover:text-[#00A3C4] mb-1">
          {workspace.name}
        </div>
        {workspace.description && (
          <div className="text-[9px] text-gray-400 leading-snug line-clamp-2">
            {workspace.description}
          </div>
        )}
        {workspace.status === "draft" && (
          <div className="mt-2">
            <span className="text-[8px] font-bold uppercase border border-yellow-400 bg-yellow-50 text-yellow-700 px-1.5 py-0.5">
              草稿
            </span>
          </div>
        )}
        {workspace.status === "reviewing" && (
          <div className="mt-2">
            <span className="text-[8px] font-bold uppercase border border-blue-400 bg-blue-50 text-blue-700 px-1.5 py-0.5">
              审核中
            </span>
          </div>
        )}
      </button>
    </Form>
  );
}

export default function ChatIndex() {
  const { workspaces, user } = useLoaderData<typeof loader>() as {
    workspaces: Workspace[];
    user: any;
  };
  const navigation = useNavigation();
  const isLoading = navigation.state !== "idle";

  const published = workspaces.filter((w) => w.status === "published");
  const myDrafts = workspaces.filter(
    (w) => (w.status === "draft" || w.status === "reviewing") && w.created_by === user.id
  );

  const categories = Array.from(new Set(published.map((w) => w.category)));

  const isEmployee = user.role === "employee";
  const draftCount = myDrafts.filter((w) => w.status === "draft").length;

  return (
    <div className="flex flex-col h-full bg-[#F0F4F8] overflow-y-auto">
      <div className="px-8 pt-8 pb-16 max-w-5xl mx-auto w-full">
        {/* Header */}
        <div className="mb-8">
          <div className="flex items-center gap-2 mb-2">
            <div className="w-2 h-6 bg-[#00D1FF]" />
            <h1 className="text-xl font-bold uppercase tracking-widest text-[#1A202C]">
              选择工作台
            </h1>
          </div>
          <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400 ml-4">
            选择一个智能体工作台，或直接开始通用对话
          </p>
        </div>

        {/* Default chat */}
        <div className="mb-8">
          <p className="text-[9px] font-bold uppercase tracking-widest text-[#00A3C4] mb-3">— 通用</p>
          <Form method="post">
            <button
              type="submit"
              disabled={isLoading}
              className="border-2 border-dashed border-[#1A202C] bg-white p-4 hover:border-[#00D1FF] hover:bg-[#EBF4F7] transition-colors group text-left w-64 disabled:opacity-50"
            >
              <div className="w-8 h-8 flex items-center justify-center text-lg mb-3 border-2 border-dashed border-[#1A202C] bg-[#F0F4F8]">
                {isLoading ? "…" : "✦"}
              </div>
              <div className="text-[10px] font-bold uppercase tracking-wide text-[#1A202C] group-hover:text-[#00A3C4] mb-1">
                自由对话
              </div>
              <div className="text-[9px] text-gray-400 leading-snug">
                AI 自动匹配合适的 Skill 和知识
              </div>
            </button>
          </Form>
        </div>

        {/* Published workspaces by category */}
        {categories.map((cat) => {
          const catWorkspaces = published.filter((w) => w.category === cat);
          return (
            <div key={cat} className="mb-8">
              <p className="text-[9px] font-bold uppercase tracking-widest text-[#00A3C4] mb-3">
                — {cat}
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
                {catWorkspaces.map((ws) => (
                  <WorkspaceCard key={ws.id} workspace={ws} />
                ))}
              </div>
            </div>
          );
        })}

        {/* My draft workspaces */}
        {myDrafts.length > 0 && (
          <div className="mb-8">
            <div className="flex items-center justify-between mb-3">
              <p className="text-[9px] font-bold uppercase tracking-widest text-[#00A3C4]">
                — 我的工作台
              </p>
              <a
                href="/workspaces/my"
                className="text-[9px] font-bold uppercase text-gray-400 hover:text-[#00A3C4]"
              >
                管理 &gt;
              </a>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
              {myDrafts.map((ws) => (
                <WorkspaceCard key={ws.id} workspace={ws} />
              ))}
            </div>
          </div>
        )}

        {/* Create new workspace (employee, ≤3 drafts) */}
        {isEmployee && draftCount < 3 && (
          <div className="mb-4">
            {myDrafts.length === 0 && (
              <p className="text-[9px] font-bold uppercase tracking-widest text-[#00A3C4] mb-3">— 我的工作台</p>
            )}
            <a
              href="/workspaces/my"
              className="inline-flex items-center gap-2 border-2 border-dashed border-[#1A202C] bg-white px-4 py-3 text-[10px] font-bold uppercase tracking-wide text-gray-500 hover:border-[#00D1FF] hover:text-[#00A3C4] hover:bg-[#EBF4F7] transition-colors"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M12 4v16m8-8H4" />
              </svg>
              新建自定义工作台 ({draftCount}/3)
            </a>
          </div>
        )}
      </div>
    </div>
  );
}
