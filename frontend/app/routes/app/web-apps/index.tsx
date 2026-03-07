import { useState } from "react";
import { useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

interface WebApp {
  id: number;
  name: string;
  description: string;
  is_public: boolean;
  preview_url: string;
  share_url: string | null;
  share_token: string | null;
  created_at: string;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const apps = await apiFetch("/api/web-apps", { token });
  return { apps, token };
}

export default function WebAppsIndex() {
  const { apps, token } = useLoaderData<typeof loader>() as { apps: WebApp[]; token: string };
  const [previewId, setPreviewId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [appList, setAppList] = useState<WebApp[]>(apps || []);

  async function deleteApp(id: number) {
    if (!confirm("确认删除该小工具？")) return;
    setDeleting(id);
    try {
      await fetch(`/api/web-apps/${id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      setAppList((prev) => prev.filter((a) => a.id !== id));
    } finally {
      setDeleting(null);
    }
  }

  async function togglePublic(app: WebApp) {
    try {
      const resp = await fetch(`/api/web-apps/${app.id}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ is_public: !app.is_public }),
      });
      const updated = await resp.json();
      setAppList((prev) => prev.map((a) => (a.id === app.id ? { ...a, ...updated } : a)));
    } catch (e) {
      console.error(e);
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">我的小工具</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">通过对话生成的 Web 小工具</p>
        </div>
      </div>

      <div className="p-6 max-w-5xl">
        {appList.length === 0 ? (
          <div className="pixel-border bg-white p-12 text-center">
            <p className="text-xs font-bold uppercase text-gray-400">还没有小工具</p>
            <p className="text-[10px] font-bold uppercase text-gray-300 mt-1">
              在对话中说"帮我搭一个计算器"即可生成
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {appList.map((app) => (
              <div key={app.id} className="pixel-border bg-white flex flex-col">
                <div className="bg-[#2D3748] text-white px-4 py-2 flex items-center justify-between">
                  <span className="text-[9px] font-bold uppercase truncate max-w-[140px]">{app.name}</span>
                  {app.is_public && (
                    <span className="text-[9px] font-bold uppercase text-[#00D1FF] ml-2 flex-shrink-0">[公开]</span>
                  )}
                </div>
                <div className="flex-1 p-4">
                  {app.description && (
                    <p className="text-[10px] text-gray-500 line-clamp-2 mb-2">{app.description}</p>
                  )}
                  <p className="text-[9px] font-bold uppercase text-gray-400">
                    {new Date(app.created_at).toLocaleDateString("zh-CN")}
                  </p>
                </div>
                <div className="flex items-center gap-0 border-t-2 border-[#1A202C]">
                  <button
                    onClick={() => setPreviewId(app.id)}
                    className="flex-1 py-2 text-[9px] font-bold uppercase text-[#00A3C4] hover:bg-[#CCF2FF] border-r-2 border-[#1A202C] transition-colors"
                  >
                    预览
                  </button>
                  {app.share_token && (
                    <button
                      onClick={() => {
                        const url = `${window.location.origin}/share/${app.share_token}`;
                        navigator.clipboard.writeText(url);
                        alert("分享链接已复制");
                      }}
                      className="flex-1 py-2 text-[9px] font-bold uppercase text-gray-600 hover:bg-[#EBF4F7] border-r-2 border-[#1A202C] transition-colors"
                    >
                      复制链接
                    </button>
                  )}
                  <button
                    onClick={() => deleteApp(app.id)}
                    disabled={deleting === app.id}
                    className="flex-1 py-2 text-[9px] font-bold uppercase text-red-500 hover:bg-red-50 disabled:opacity-50 transition-colors"
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Preview Modal */}
      {previewId && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="pixel-border bg-white w-full max-w-4xl h-[80vh] flex flex-col">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
              <span className="text-[10px] font-bold uppercase tracking-widest">App_Preview</span>
              <div className="flex items-center gap-4">
                <a
                  href={`/api/web-apps/${previewId}/preview`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[10px] font-bold uppercase text-[#00D1FF] hover:underline"
                >
                  新窗口打开
                </a>
                <button
                  onClick={() => setPreviewId(null)}
                  className="text-[10px] font-bold uppercase text-gray-400 hover:text-white"
                >
                  [关闭]
                </button>
              </div>
            </div>
            <iframe
              src={`/api/web-apps/${previewId}/preview`}
              className="flex-1"
              title="Web App Preview"
            />
          </div>
        </div>
      )}
    </div>
  );
}
