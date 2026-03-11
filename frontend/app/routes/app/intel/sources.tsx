import { useLoaderData } from "react-router";
import type { Route } from "./+types/sources";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

interface IntelSource {
  id: number;
  name: string;
  source_type: "rss" | "crawler" | "webhook" | "manual";
  config: Record<string, unknown>;
  schedule: string | null;
  is_active: boolean;
  last_run_at: string | null;
  created_at: string;
  managed_by: number | null;
  authorized_user_ids: number[];
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const sources = await apiFetch("/api/intel/sources?mine=true", { token });
  return { sources, user };
}

const TYPE_LABELS: Record<string, string> = {
  rss:     "RSS",
  crawler: "爬虫",
  webhook: "Webhook",
  manual:  "手动",
};

const TYPE_COLORS: Record<string, string> = {
  rss:     "bg-blue-50 text-blue-700 border-blue-300",
  crawler: "bg-orange-50 text-orange-700 border-orange-300",
  webhook: "bg-purple-50 text-purple-700 border-purple-300",
  manual:  "bg-gray-50 text-gray-600 border-gray-300",
};

export default function MyIntelSources() {
  const { sources } = useLoaderData<typeof loader>() as { sources: IntelSource[]; user: any };

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">我管理的数据源</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">外部采集源 — 需由管理员授权</p>
          </div>
        </div>
      </div>

      <div className="p-6 max-w-4xl">
        {sources.length === 0 ? (
          <div className="pixel-border bg-white p-12 text-center">
            <p className="text-xs font-bold uppercase text-gray-400">暂无授权数据源</p>
            <p className="text-[10px] text-gray-400 mt-2 uppercase">
              如需管理数据源，请联系管理员进行线下申请和授权
            </p>
          </div>
        ) : (
          <div className="pixel-border bg-white overflow-hidden">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
              <span className="text-[10px] font-bold uppercase tracking-widest">
                Intel_Sources ({sources.length})
              </span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>

            <table className="w-full text-left">
              <thead>
                <tr className="border-b-2 border-[#1A202C] bg-[#F0F4F8]">
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">名称</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">类型</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">采集频率</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">最近采集</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {sources.map((s) => (
                  <tr key={s.id} className="hover:bg-[#F0F4F8] transition-colors">
                    <td className="py-3 px-4">
                      <div className="text-xs font-bold text-[#1A202C]">{s.name}</div>
                      {!!s.config?.url && (
                        <div className="text-[9px] text-gray-400 mt-0.5 font-mono truncate max-w-xs">
                          {s.config.url as string}
                        </div>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${TYPE_COLORS[s.source_type] || "bg-gray-50 text-gray-600 border-gray-300"}`}>
                        {TYPE_LABELS[s.source_type] || s.source_type}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold uppercase text-gray-500">
                      {s.schedule || "—"}
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${
                        s.is_active
                          ? "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]"
                          : "bg-gray-100 text-gray-500 border-gray-300"
                      }`}>
                        {s.is_active ? "启用" : "停用"}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold text-gray-400 uppercase">
                      {s.last_run_at
                        ? new Date(s.last_run_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
                        : "从未采集"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-4 border-2 border-[#1A202C] bg-[#EBF4F7] px-4 py-3">
          <p className="text-[9px] font-bold uppercase text-[#00A3C4] tracking-widest">
            — 关于数据源权限
          </p>
          <p className="text-[10px] text-gray-600 mt-1 font-bold">
            数据源需要线下向管理员申请，获批后管理员将你设置为负责人（managed_by）或授权操作者（authorized_users）。
            你在此只能查看和监控已授权的数据源采集状态，修改配置需联系超级管理员。
          </p>
        </div>
      </div>
    </div>
  );
}
