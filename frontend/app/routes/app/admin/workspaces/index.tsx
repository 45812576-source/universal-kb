import { useState } from "react";
import { Link, useLoaderData, useRevalidator } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

const STATUS_STYLE: Record<string, string> = {
  draft:     "bg-gray-100 text-gray-600 border-gray-400",
  reviewing: "bg-yellow-100 text-yellow-700 border-yellow-400",
  published: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]",
  archived:  "bg-red-100 text-red-600 border-red-400",
};
const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", reviewing: "审核中", published: "已发布", archived: "已归档",
};

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const workspaces = await apiFetch("/api/workspaces", { token });
  return { workspaces, token, user };
}

export default function AdminWorkspacesIndex() {
  const { workspaces, token, user } = useLoaderData<typeof loader>() as {
    workspaces: any[];
    token: string;
    user: any;
  };
  const revalidator = useRevalidator();
  const [error, setError] = useState("");
  const [reviewing, setReviewing] = useState<number | null>(null);

  const reviewing_ws = workspaces.filter((w) => w.status === "reviewing");
  const other_ws = workspaces.filter((w) => w.status !== "reviewing");

  async function doReview(wsId: number, action: "approve" | "reject") {
    setError("");
    try {
      await apiFetch(`/api/workspaces/${wsId}/review`, {
        method: "PATCH",
        body: JSON.stringify({ action }),
        token,
      });
      setReviewing(null);
      revalidator.revalidate();
    } catch (e: any) {
      setError(e.message || "操作失败");
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">工作台管理</h1>
        </div>
        <Link
          to="/admin/workspaces/new"
          className="bg-[#1A202C] text-white px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
        >
          + 新建工作台
        </Link>
      </div>

      <div className="p-6 max-w-5xl space-y-6">
        {error && (
          <div className="border-2 border-red-400 bg-red-50 px-4 py-2 text-xs font-bold text-red-700 uppercase">
            [ERROR] {error}
          </div>
        )}

        {/* Pending review */}
        {reviewing_ws.length > 0 && (
          <div>
            <p className="text-[9px] font-bold uppercase tracking-widest text-yellow-600 mb-3">
              — 待审核 ({reviewing_ws.length})
            </p>
            <div className="space-y-2">
              {reviewing_ws.map((ws) => (
                <div key={ws.id} className="border-2 border-yellow-400 bg-yellow-50 p-4 flex items-center justify-between">
                  <div>
                    <div className="flex items-center gap-2 mb-1">
                      <span
                        className="inline-block w-4 h-4 border border-[#1A202C]"
                        style={{ backgroundColor: ws.color }}
                      />
                      <span className="text-xs font-bold text-[#1A202C]">{ws.name}</span>
                      <span className="text-[9px] font-bold uppercase text-gray-400">{ws.category}</span>
                    </div>
                    {ws.description && (
                      <p className="text-[10px] text-gray-500 ml-6">{ws.description}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0 ml-4">
                    <Link
                      to={`/admin/workspaces/${ws.id}`}
                      className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                    >
                      详情
                    </Link>
                    {reviewing !== ws.id ? (
                      <button
                        onClick={() => setReviewing(ws.id)}
                        className="bg-[#1A202C] text-white px-3 py-1 text-[10px] font-bold uppercase hover:bg-black"
                      >
                        审核
                      </button>
                    ) : (
                      <div className="flex gap-1.5">
                        <button
                          onClick={() => doReview(ws.id, "approve")}
                          className="bg-[#00CC99] text-white px-3 py-1 text-[10px] font-bold uppercase hover:bg-green-600"
                        >
                          通过
                        </button>
                        <button
                          onClick={() => doReview(ws.id, "reject")}
                          className="bg-red-500 text-white px-3 py-1 text-[10px] font-bold uppercase hover:bg-red-700"
                        >
                          驳回
                        </button>
                        <button
                          onClick={() => setReviewing(null)}
                          className="text-[10px] font-bold uppercase text-gray-400"
                        >
                          取消
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* All workspaces */}
        <div>
          <p className="text-[9px] font-bold uppercase tracking-widest text-[#00A3C4] mb-3">— 全部工作台</p>
          {workspaces.length === 0 ? (
            <p className="text-xs font-bold uppercase text-gray-400">暂无工作台</p>
          ) : (
            <div className="pixel-border bg-white overflow-hidden">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-[#2D3748] text-white border-b-2 border-[#1A202C]">
                    <th className="text-left px-4 py-2.5 text-[9px] font-bold uppercase tracking-widest">名称</th>
                    <th className="text-left px-4 py-2.5 text-[9px] font-bold uppercase tracking-widest">分类</th>
                    <th className="text-left px-4 py-2.5 text-[9px] font-bold uppercase tracking-widest">可见性</th>
                    <th className="text-left px-4 py-2.5 text-[9px] font-bold uppercase tracking-widest">状态</th>
                    <th className="text-left px-4 py-2.5 text-[9px] font-bold uppercase tracking-widest">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {workspaces.map((ws) => (
                    <tr key={ws.id} className="hover:bg-[#F0F4F8]">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <span
                            className="inline-block w-3 h-3 border border-gray-300"
                            style={{ backgroundColor: ws.color }}
                          />
                          <span className="font-bold text-[#1A202C]">{ws.name}</span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-gray-500 text-[10px] font-bold uppercase">{ws.category}</td>
                      <td className="px-4 py-3 text-gray-500 text-[10px] font-bold uppercase">
                        {ws.visibility === "all" ? "全员" : "部门内"}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${STATUS_STYLE[ws.status] || ""}`}>
                          {STATUS_LABEL[ws.status] || ws.status}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <Link
                          to={`/admin/workspaces/${ws.id}`}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          编辑
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
