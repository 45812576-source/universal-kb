import { useEffect, useState } from "react";
import { useLoaderData } from "react-router";
import type { Route } from "./+types/audit";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  if (user.role !== "super_admin" && user.role !== "dept_admin") {
    throw new Response("Forbidden", { status: 403 });
  }
  const logs = await apiFetch("/api/audit-logs?page_size=20", { token });
  return { logs, token, user };
}

export default function AuditPage() {
  const { logs: initial, token } = useLoaderData<typeof loader>() as any;
  const [logs, setLogs] = useState(initial);
  const [filters, setFilters] = useState({ table_name: "", operation: "", page: 1 });
  const [loading, setLoading] = useState(false);

  async function fetchLogs(f = filters) {
    setLoading(true);
    const params = new URLSearchParams();
    if (f.table_name) params.set("table_name", f.table_name);
    if (f.operation) params.set("operation", f.operation);
    params.set("page", String(f.page));
    params.set("page_size", "20");
    try {
      const data = await apiFetch(`/api/audit-logs?${params}`, { token });
      setLogs(data);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">操作审计日志</h1>
      </div>

      <div className="p-6 max-w-6xl">
        {/* Filters */}
        <div className="pixel-border bg-white p-4 mb-4 flex gap-4 items-end">
          <div>
            <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">数据表</label>
            <input
              value={filters.table_name}
              onChange={(e) => setFilters((f) => ({ ...f, table_name: e.target.value }))}
              placeholder="表名"
              className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
            />
          </div>
          <div>
            <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">操作类型</label>
            <select
              value={filters.operation}
              onChange={(e) => setFilters((f) => ({ ...f, operation: e.target.value }))}
              className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
            >
              <option value="">全部</option>
              <option value="INSERT">INSERT</option>
              <option value="UPDATE">UPDATE</option>
              <option value="DELETE">DELETE</option>
            </select>
          </div>
          <button
            onClick={() => {
              const f = { ...filters, page: 1 };
              setFilters(f);
              fetchLogs(f);
            }}
            className="bg-[#1A202C] text-white px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
          >
            查询
          </button>
        </div>

        {/* Table */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Audit_Log</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <table className="w-full text-left">
            <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
              <tr>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">时间</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">操作人</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">数据表</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">操作</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">行ID</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">变更前</th>
                <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">变更后</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {loading && (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-xs font-bold uppercase text-gray-400">加载中...</td>
                </tr>
              )}
              {!loading && logs?.logs?.map((log: any) => (
                <tr key={log.id} className="hover:bg-[#F0F4F8]">
                  <td className="px-4 py-3 text-[9px] font-bold text-gray-500 whitespace-nowrap">
                    {log.created_at ? new Date(log.created_at).toLocaleString("zh-CN") : "-"}
                  </td>
                  <td className="px-4 py-3 text-[10px] font-bold text-gray-700">{log.user_id ?? "-"}</td>
                  <td className="px-4 py-3 text-[10px] font-bold font-mono text-gray-700">{log.table_name}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${
                      log.operation === "INSERT" ? "bg-green-100 text-green-700 border-green-400" :
                      log.operation === "UPDATE" ? "bg-yellow-100 text-yellow-700 border-yellow-400" :
                      "bg-red-100 text-red-700 border-red-400"
                    }`}>
                      {log.operation}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-[10px] font-bold text-gray-500">{log.row_id ?? "-"}</td>
                  <td className="px-4 py-3 text-[9px] text-gray-500 max-w-xs truncate font-mono">
                    {log.old_values ? JSON.stringify(log.old_values).slice(0, 80) : "-"}
                  </td>
                  <td className="px-4 py-3 text-[9px] text-gray-500 max-w-xs truncate font-mono">
                    {log.new_values ? JSON.stringify(log.new_values).slice(0, 80) : "-"}
                  </td>
                </tr>
              ))}
              {!loading && !logs?.logs?.length && (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-xs font-bold uppercase text-gray-400">暂无审计日志</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {logs?.total > 20 && (
          <div className="mt-4 flex items-center gap-3 justify-center">
            <button
              disabled={filters.page <= 1}
              onClick={() => {
                const f = { ...filters, page: filters.page - 1 };
                setFilters(f);
                fetchLogs(f);
              }}
              className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-[10px] font-bold uppercase text-gray-600 hover:bg-[#EBF4F7] disabled:opacity-40"
            >
              &lt; 上一页
            </button>
            <span className="text-[10px] font-bold uppercase text-gray-500">
              第 {filters.page} 页 / 共 {Math.ceil(logs.total / 20)} 页
            </span>
            <button
              disabled={filters.page >= Math.ceil(logs.total / 20)}
              onClick={() => {
                const f = { ...filters, page: filters.page + 1 };
                setFilters(f);
                fetchLogs(f);
              }}
              className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-[10px] font-bold uppercase text-gray-600 hover:bg-[#EBF4F7] disabled:opacity-40"
            >
              下一页 &gt;
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
