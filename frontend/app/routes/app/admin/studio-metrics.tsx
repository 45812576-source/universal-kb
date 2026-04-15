import { useMemo, useState } from "react";
import { useLoaderData } from "react-router";
import type { Route } from "./+types/studio-metrics";
import { requireUser } from "~/lib/auth.server";
import { API_BASE, apiFetch } from "~/lib/api";

type MetricBucket = {
  count?: number;
  p50_s?: number | null;
  p75_s?: number | null;
  p90_s?: number | null;
  missing_after_start?: number;
};

type StudioRunRecord = {
  run_id: string;
  created_at?: string | null;
  status?: string | null;
  user_id?: number | null;
  workspace_id?: number | null;
  error?: string | null;
  metadata?: {
    latency?: Record<string, string | null | undefined>;
    rollout?: {
      eligible?: boolean;
      scope?: string;
      reason?: string;
      flags?: Record<string, boolean>;
    };
  };
};

type Dashboard = {
  window_days: number;
  run_count: number;
  status_counts: Record<string, number>;
  first_useful_response: MetricBucket;
  deep_completed: MetricBucket;
  first_token: MetricBucket;
  quality_proxy: {
    deep_completion_rate?: number | null;
    run_failure_rate?: number | null;
    completion_rate?: number | null;
  };
  runtime_snapshot?: Record<string, unknown>;
  records: StudioRunRecord[];
};

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  if (user.role !== "super_admin") {
    throw new Response("Forbidden", { status: 403 });
  }
  const dashboard = await apiFetch("/api/admin/studio/metrics?days=7&limit=200", { token });
  return { dashboard, token };
}

function formatSeconds(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(value >= 10 ? 1 : 2)}s`;
}

function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function formatDate(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN");
}

function durationSeconds(start: string | null | undefined, end: string | null | undefined) {
  if (!start || !end) return null;
  const startAt = new Date(start).getTime();
  const endAt = new Date(end).getTime();
  if (Number.isNaN(startAt) || Number.isNaN(endAt) || endAt < startAt) return null;
  return (endAt - startAt) / 1000;
}

function statusClass(status: string | null | undefined) {
  if (status === "completed") return "bg-green-100 text-green-700 border-green-400";
  if (status === "failed") return "bg-red-100 text-red-700 border-red-400";
  if (status === "running") return "bg-blue-100 text-blue-700 border-blue-400";
  if (status === "superseded") return "bg-yellow-100 text-yellow-700 border-yellow-400";
  return "bg-gray-100 text-gray-600 border-gray-300";
}

function MetricCard({ label, value, hint, tone = "cyan" }: { label: string; value: string; hint: string; tone?: "cyan" | "green" | "red" | "purple" }) {
  const toneClass = {
    cyan: "bg-[#CCF2FF] text-[#007A96] border-[#00A3C4]",
    green: "bg-green-100 text-green-700 border-green-400",
    red: "bg-red-100 text-red-700 border-red-400",
    purple: "bg-purple-100 text-purple-700 border-purple-400",
  }[tone];
  return (
    <div className="pixel-border bg-white p-4">
      <div className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest ${toneClass}`}>
        {label}
      </div>
      <div className="mt-3 text-2xl font-bold text-[#1A202C]">{value}</div>
      <div className="mt-1 text-[10px] font-bold uppercase tracking-wide text-gray-400">{hint}</div>
    </div>
  );
}

function PercentilePanel({ title, bucket }: { title: string; bucket: MetricBucket }) {
  return (
    <div className="pixel-border bg-white overflow-hidden">
      <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C]">
        <span className="text-[10px] font-bold uppercase tracking-widest">{title}</span>
      </div>
      <div className="grid grid-cols-4 divide-x divide-gray-100">
        {[
          ["count", String(bucket.count ?? 0)],
          ["p50", formatSeconds(bucket.p50_s)],
          ["p75", formatSeconds(bucket.p75_s)],
          ["p90", formatSeconds(bucket.p90_s)],
        ].map(([label, value]) => (
          <div key={label} className="p-4">
            <div className="text-[9px] font-bold uppercase tracking-widest text-gray-400">{label}</div>
            <div className="mt-1 text-lg font-bold text-[#1A202C]">{value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function StudioMetricsPage() {
  const { dashboard: initialDashboard, token } = useLoaderData<typeof loader>() as { dashboard: Dashboard; token: string };
  const [dashboard, setDashboard] = useState<Dashboard>(initialDashboard);
  const [days, setDays] = useState(initialDashboard.window_days || 7);
  const [limit, setLimit] = useState(200);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const statusEntries = useMemo(
    () => Object.entries(dashboard.status_counts || {}).sort((a, b) => b[1] - a[1]),
    [dashboard.status_counts],
  );

  async function refreshMetrics(nextDays = days, nextLimit = limit) {
    setLoading(true);
    try {
      const data = await apiFetch(`/api/admin/studio/metrics?days=${nextDays}&limit=${nextLimit}`, { token });
      setDashboard(data);
    } finally {
      setLoading(false);
    }
  }

  async function downloadCsv() {
    setDownloading(true);
    try {
      const resp = await fetch(`${API_BASE}/api/admin/studio/metrics/export?days=${days}&limit=${limit}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) throw new Error(await resp.text());
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `skill-studio-metrics-${days}d.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">Skill Studio 监控</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">SLA / Deep Lane / Rollout 运行面板</p>
          </div>
        </div>
        <div className="flex items-end gap-3">
          <div>
            <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1">窗口</label>
            <select
              value={days}
              onChange={(event) => setDays(Number(event.target.value))}
              className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
            >
              {[1, 3, 7, 14, 30].map((value) => <option key={value} value={value}>{value} 天</option>)}
            </select>
          </div>
          <div>
            <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1">数量</label>
            <select
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
              className="border-2 border-[#1A202C] bg-white px-3 py-1.5 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
            >
              {[100, 200, 500, 1000].map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </div>
          <button
            onClick={() => refreshMetrics()}
            disabled={loading}
            className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors disabled:opacity-50"
          >
            {loading ? "刷新中" : "刷新"}
          </button>
          <button
            onClick={downloadCsv}
            disabled={downloading}
            className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-[#1A202C] hover:bg-[#CCF2FF] transition-colors disabled:opacity-50"
          >
            {downloading ? "导出中" : "导出 CSV"}
          </button>
        </div>
      </div>

      <div className="p-6 space-y-5">
        <div className="grid grid-cols-4 gap-4">
          <MetricCard label="Runs" value={String(dashboard.run_count || 0)} hint={`${dashboard.window_days} 天窗口`} />
          <MetricCard label="SLA P90" value={formatSeconds(dashboard.first_useful_response?.p90_s)} hint="first useful response" tone="purple" />
          <MetricCard label="Deep Rate" value={formatPercent(dashboard.quality_proxy?.deep_completion_rate)} hint="deep completion" tone="green" />
          <MetricCard label="Failure" value={formatPercent(dashboard.quality_proxy?.run_failure_rate)} hint="run failure rate" tone="red" />
        </div>

        <div className="grid grid-cols-3 gap-4">
          <PercentilePanel title="First_Useful_Response" bucket={dashboard.first_useful_response || {}} />
          <PercentilePanel title="First_Token" bucket={dashboard.first_token || {}} />
          <PercentilePanel title="Deep_Completed" bucket={dashboard.deep_completed || {}} />
        </div>

        <div className="grid grid-cols-3 gap-4">
          <div className="pixel-border bg-white p-4">
            <div className="text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-3">Run Status</div>
            <div className="space-y-2">
              {statusEntries.length === 0 && <div className="text-xs font-bold text-gray-400">暂无 run 数据</div>}
              {statusEntries.map(([status, count]) => (
                <div key={status} className="flex items-center justify-between">
                  <span className={`border px-2 py-0.5 text-[9px] font-bold uppercase ${statusClass(status)}`}>{status}</span>
                  <span className="text-xs font-bold text-[#1A202C]">{count}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="pixel-border bg-white p-4">
            <div className="text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-3">Quality Proxy</div>
            <div className="space-y-2 text-xs font-bold text-[#1A202C]">
              <div className="flex justify-between"><span>completion_rate</span><span>{formatPercent(dashboard.quality_proxy?.completion_rate)}</span></div>
              <div className="flex justify-between"><span>deep_missing_after_start</span><span>{dashboard.deep_completed?.missing_after_start ?? 0}</span></div>
              <div className="flex justify-between"><span>first_useful_count</span><span>{dashboard.first_useful_response?.count ?? 0}</span></div>
            </div>
          </div>
          <div className="pixel-border bg-white p-4">
            <div className="text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-3">Runtime Snapshot</div>
            <pre className="max-h-28 overflow-auto text-[10px] leading-5 text-gray-500 font-mono bg-[#F0F4F8] border border-gray-200 p-2">
              {JSON.stringify(dashboard.runtime_snapshot || {}, null, 2)}
            </pre>
          </div>
        </div>

        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Recent_Skill_Studio_Runs</span>
            <span className="text-[9px] font-bold uppercase text-gray-300">{dashboard.records?.length || 0} records</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
                <tr>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">时间</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">Run</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">首答</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">Deep</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">Rollout</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">Flags</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {(dashboard.records || []).map((record) => {
                  const latency = record.metadata?.latency || {};
                  const rollout = record.metadata?.rollout;
                  const flags = Object.entries(rollout?.flags || {})
                    .filter(([, enabled]) => enabled)
                    .map(([key]) => key.replace("_enabled", ""))
                    .join(", ");
                  return (
                    <tr key={record.run_id} className="hover:bg-[#F0F4F8]">
                      <td className="px-4 py-3 text-[9px] font-bold text-gray-500 whitespace-nowrap">{formatDate(record.created_at)}</td>
                      <td className="px-4 py-3 text-[10px] font-bold font-mono text-gray-700 max-w-[180px] truncate">{record.run_id}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${statusClass(record.status)}`}>
                          {record.status || "unknown"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-[10px] font-bold text-gray-700">
                        {formatSeconds(durationSeconds(latency.request_accepted_at, latency.first_useful_response_at))}
                      </td>
                      <td className="px-4 py-3 text-[10px] font-bold text-gray-700">
                        {formatSeconds(durationSeconds(latency.request_accepted_at, latency.deep_completed_at))}
                      </td>
                      <td className="px-4 py-3 text-[10px] font-bold text-gray-500">
                        {rollout?.scope || "—"}
                      </td>
                      <td className="px-4 py-3 text-[9px] font-mono text-gray-500 max-w-[260px] truncate">
                        {flags || "—"}
                      </td>
                    </tr>
                  );
                })}
                {!dashboard.records?.length && (
                  <tr>
                    <td colSpan={7} className="px-4 py-10 text-center text-xs font-bold uppercase text-gray-400">
                      暂无 Skill Studio run 数据
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
