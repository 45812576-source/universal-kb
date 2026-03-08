import { useLoaderData, useSearchParams } from "react-router";
import type { Route } from "./+types/my";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { KnowledgeEntry } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const entries = await apiFetch("/api/knowledge", { token });
  return { entries };
}

const STATUS_INFO: Record<string, { label: string; color: string }> = {
  pending:  { label: "待审核", color: "bg-yellow-100 text-yellow-800 border-yellow-400" },
  approved: { label: "已通过", color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
  rejected: { label: "已拒绝", color: "bg-red-100 text-red-700 border-red-400" },
  archived: { label: "已归档", color: "bg-gray-100 text-gray-500 border-gray-400" },
};

const CATEGORY_LABELS: Record<string, string> = {
  experience: "经验总结",
  methodology: "方法论",
  case_study: "案例",
  data: "数据资产",
  template: "模板",
  external: "外部资料",
};

export default function MyKnowledge() {
  const { entries } = useLoaderData<typeof loader>() as { entries: KnowledgeEntry[] };
  const [searchParams] = useSearchParams();
  const justSubmitted = searchParams.get("submitted");

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">我的知识</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">查看提交的所有知识条目</p>
        </div>
      </div>

      <div className="p-6 max-w-5xl">
        {justSubmitted && (
          <div className="mb-4 border-2 border-[#00D1FF] bg-[#CCF2FF]/30 px-4 py-3 text-xs font-bold text-[#00A3C4] uppercase">
            [OK] 提交成功！已进入审核队列，管理员审核后将自动入库。
          </div>
        )}

        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Knowledge_List</span>
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
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">标签</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">提交时间</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {entries.map((e) => {
                const si = STATUS_INFO[e.status] || { label: e.status, color: "bg-gray-100 text-gray-500 border-gray-400" };
                const allTags = [...e.industry_tags, ...e.platform_tags, ...e.topic_tags].slice(0, 4);
                return (
                  <tr key={e.id} className="hover:bg-[#F0F4F8] transition-colors">
                    <td className="py-3 px-4">
                      <div className="text-xs font-bold text-[#1A202C] truncate max-w-xs">{e.title}</div>
                      <div className="text-[10px] text-gray-400 mt-0.5 truncate max-w-xs">{e.content}</div>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold uppercase text-gray-500">
                      {CATEGORY_LABELS[e.category] || e.category}
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
                  </tr>
                );
              })}
              {entries.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-12 text-center text-xs font-bold uppercase text-gray-400">
                    暂无数据 —{" "}
                    <a href="/knowledge/new" className="text-[#00A3C4] hover:underline">
                      去录入第一条
                    </a>
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
