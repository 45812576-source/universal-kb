import { Link, useLoaderData } from "react-router";
import type { Route } from "./+types/my";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending:  { label: "待审核",   color: "bg-gray-100 text-gray-600 border-gray-400" },
  adopted:  { label: "已采纳",   color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
  partial:  { label: "部分采纳", color: "bg-blue-100 text-blue-700 border-blue-400" },
  rejected: { label: "未采纳",   color: "bg-red-100 text-red-600 border-red-400" },
};

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const suggestions = await apiFetch("/api/my/suggestions", { token });
  return { suggestions, token };
}

export default function MySuggestionsPage() {
  const { suggestions } = useLoaderData<typeof loader>() as any;

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">我的改进建议</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">Skill 反馈追踪</p>
          </div>
        </div>
        <Link
          to="/suggestions/new"
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
        >
          + 提交新建议
        </Link>
      </div>

      <div className="p-6 max-w-3xl space-y-3">
        {(suggestions || []).map((s: any) => {
          const statusInfo = STATUS_LABELS[s.status] ?? STATUS_LABELS.pending;
          return (
            <div key={s.id} className="pixel-border bg-white">
              <div className="flex items-start justify-between gap-4 p-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-2">
                    <span className="text-[9px] font-bold uppercase text-gray-400 border border-gray-300 px-1.5 py-0.5">
                      SKILL #{s.skill_id}
                    </span>
                    <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${statusInfo.color}`}>
                      {statusInfo.label}
                    </span>
                  </div>
                  <p className="text-xs font-bold text-[#1A202C] mb-1">{s.problem_desc}</p>
                  <p className="text-[10px] text-gray-500 line-clamp-2">{s.expected_direction}</p>
                  {s.review_note && (
                    <div className="mt-2 border-2 border-[#00D1FF] bg-[#CCF2FF]/20 px-3 py-2 text-[10px] font-bold text-[#00A3C4] uppercase">
                      [负责人回复] {s.review_note}
                    </div>
                  )}
                </div>
                <span className="text-[9px] font-bold uppercase text-gray-400 flex-shrink-0">
                  {s.created_at ? new Date(s.created_at).toLocaleDateString("zh-CN") : ""}
                </span>
              </div>
            </div>
          );
        })}

        {(!suggestions || suggestions.length === 0) && (
          <div className="pixel-border bg-white p-8 text-center">
            <p className="text-xs font-bold uppercase text-gray-400 mb-3">暂无改进建议</p>
            <Link to="/suggestions/new" className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">
              立即提交第一条建议 &gt;
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
