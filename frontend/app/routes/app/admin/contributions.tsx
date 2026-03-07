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
    <div className="p-6 max-w-5xl">
      <h1 className="text-xl font-bold text-gray-900 mb-2">贡献统计</h1>
      <p className="text-sm text-gray-500 mb-6">基于 Skill 改进意见的采纳情况，统计各员工对知识体系的贡献度。</p>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500">排名</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500">姓名</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500">部门</th>
              <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500">提交意见数</th>
              <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500">采纳率</th>
              <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500">影响力分</th>
              <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500">影响Skill数</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {(stats || []).map((row: any, i: number) => (
              <tr key={row.user_id} className="hover:bg-gray-50">
                <td className="px-4 py-3 text-sm font-bold text-gray-400">
                  {i === 0 ? "🥇" : i === 1 ? "🥈" : i === 2 ? "🥉" : `#${i + 1}`}
                </td>
                <td className="px-4 py-3 font-medium text-gray-800">{row.display_name}</td>
                <td className="px-4 py-3 text-gray-500 text-xs">
                  {row.department_id ? deptMap[row.department_id] ?? "-" : "-"}
                </td>
                <td className="px-4 py-3 text-right text-gray-700">{row.total_suggestions}</td>
                <td className="px-4 py-3 text-right">
                  <span className={`text-xs font-medium ${
                    row.adoption_rate >= 0.5 ? "text-green-600" :
                    row.adoption_rate >= 0.2 ? "text-yellow-600" : "text-gray-400"
                  }`}>
                    {row.total_suggestions > 0 ? `${Math.round(row.adoption_rate * 100)}%` : "-"}
                  </span>
                </td>
                <td className="px-4 py-3 text-right font-bold text-blue-600">
                  {row.influence_score}
                </td>
                <td className="px-4 py-3 text-right text-gray-500">{row.impacted_skills}</td>
              </tr>
            ))}
            {(!stats || stats.length === 0) && (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-gray-400">
                  暂无贡献数据。员工提交意见并被采纳后，数据将出现在这里。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-4 text-xs text-gray-400">
        影响力分计算规则：完全采纳 ×3 分，部分采纳 ×1 分
      </div>
    </div>
  );
}
