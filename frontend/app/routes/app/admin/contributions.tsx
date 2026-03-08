import { useLoaderData } from "react-router";
import type { Route } from "./+types/contributions";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  if (user.role !== "super_admin" && user.role !== "dept_admin") {
    throw new Response("Forbidden", { status: 403 });
  }
  const [stats, departments] = await Promise.all([
    apiFetch("/api/contributions/stats", { token }),
    apiFetch("/api/admin/departments", { token }),
  ]);
  return { stats, departments, token, user };
}

export default function ContributionsPage() {
  const { stats, departments } = useLoaderData<typeof loader>() as any;

  const deptMap: Record<number, string> = {};
  for (const d of departments || []) {
    deptMap[d.id] = d.name;
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">贡献统计</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">基于 Skill 改进意见采纳情况</p>
        </div>
      </div>

      <div className="p-6 max-w-5xl">
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Contribution_Leaderboard</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <table className="w-full text-left">
            <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
              <tr>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">排名</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">姓名</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">部门</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">提交意见数</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">采纳率</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">影响力分</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">影响Skill数</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {(stats || []).map((row: any, i: number) => (
                <tr key={row.user_id} className="hover:bg-[#F0F4F8]">
                  <td className="px-4 py-3 text-xs font-bold text-[#1A202C]">
                    {i === 0 ? "[#1]" : i === 1 ? "[#2]" : i === 2 ? "[#3]" : `#${i + 1}`}
                  </td>
                  <td className="px-4 py-3 text-xs font-bold text-[#1A202C]">{row.display_name}</td>
                  <td className="px-4 py-3 text-[10px] font-bold uppercase text-gray-500">
                    {row.department_id ? deptMap[row.department_id] ?? "-" : "-"}
                  </td>
                  <td className="px-4 py-3 text-right text-xs font-bold text-gray-700">{row.total_suggestions}</td>
                  <td className="px-4 py-3 text-right">
                    <span className={`text-xs font-bold uppercase ${
                      row.adoption_rate >= 0.5 ? "text-green-600" :
                      row.adoption_rate >= 0.2 ? "text-yellow-600" : "text-gray-400"
                    }`}>
                      {row.total_suggestions > 0 ? `${Math.round(row.adoption_rate * 100)}%` : "-"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right text-xs font-bold text-[#00A3C4]">
                    {row.influence_score}
                  </td>
                  <td className="px-4 py-3 text-right text-[10px] font-bold text-gray-500">{row.impacted_skills}</td>
                </tr>
              ))}
              {(!stats || stats.length === 0) && (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-xs font-bold uppercase text-gray-400">
                    暂无贡献数据
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="mt-4 text-[9px] font-bold uppercase text-gray-400">
          影响力分计算规则：完全采纳 ×3 分，部分采纳 ×1 分
        </div>
      </div>
    </div>
  );
}
