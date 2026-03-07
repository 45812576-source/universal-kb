import { useState } from "react";
import { useLoaderData, useFetcher } from "react-router";
import type { Route } from "./+types/index";
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
}

interface IntelEntry {
  id: number;
  title: string;
  industry: string | null;
  platform: string | null;
  status: "pending" | "approved" | "rejected";
  created_at: string;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const [sources, entries] = await Promise.all([
    apiFetch("/api/intel/sources", { token }),
    apiFetch("/api/intel/entries?page_size=50", { token }),
  ]);
  return { sources, entries, token };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;

  if (intent === "approve") {
    await apiFetch(`/api/intel/entries/${form.get("entryId")}/approve`, { method: "PATCH", token });
  } else if (intent === "reject") {
    await apiFetch(`/api/intel/entries/${form.get("entryId")}/reject`, { method: "PATCH", token });
  } else if (intent === "create_source") {
    const body = {
      name: form.get("name") as string,
      source_type: form.get("source_type") as string,
      config: JSON.parse((form.get("config") as string) || "{}"),
      schedule: (form.get("schedule") as string) || null,
    };
    await apiFetch("/api/intel/sources", { method: "POST", body: JSON.stringify(body), token });
  } else if (intent === "delete_source") {
    await apiFetch(`/api/intel/sources/${form.get("sourceId")}`, { method: "DELETE", token });
  }
  return null;
}

const STATUS_MAP: Record<string, { label: string; color: string }> = {
  pending: { label: "待审核", color: "bg-yellow-100 text-yellow-700" },
  approved: { label: "已通过", color: "bg-green-100 text-green-700" },
  rejected: { label: "已拒绝", color: "bg-red-100 text-red-600" },
};

export default function AdminIntelIndex() {
  const { sources, entries, token } = useLoaderData<typeof loader>() as {
    sources: IntelSource[];
    entries: { items: IntelEntry[]; total: number };
    token: string;
  };
  const fetcher = useFetcher();
  const [showCreateSource, setShowCreateSource] = useState(false);
  const [runningId, setRunningId] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<"sources" | "entries">("sources");

  async function triggerRun(sourceId: number) {
    setRunningId(sourceId);
    try {
      await fetch(`/api/intel/sources/${sourceId}/run`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      alert("采集任务已启动，请稍后刷新查看结果");
    } finally {
      setRunningId(null);
    }
  }

  return (
    <div className="p-6 max-w-6xl">
      <div className="mb-6">
        <h1 className="text-xl font-bold text-gray-900">情报管理</h1>
        <p className="text-sm text-gray-500 mt-0.5">管理数据源和审核情报条目</p>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-gray-200">
        {(["sources", "entries"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab ? "border-blue-600 text-blue-600" : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {tab === "sources" ? "数据源管理" : `情报审核 (${entries?.total || 0})`}
          </button>
        ))}
        {activeTab === "sources" && (
          <button
            onClick={() => setShowCreateSource(true)}
            className="ml-auto rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 mb-1"
          >
            + 添加数据源
          </button>
        )}
      </div>

      {/* Sources Tab */}
      {activeTab === "sources" && (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 bg-gray-50">
                <th className="text-left py-3 px-4 font-medium text-gray-600">名称</th>
                <th className="text-left py-3 px-4 font-medium text-gray-600">类型</th>
                <th className="text-left py-3 px-4 font-medium text-gray-600">计划</th>
                <th className="text-left py-3 px-4 font-medium text-gray-600">最后运行</th>
                <th className="text-right py-3 px-4 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(sources || []).map((source) => (
                <tr key={source.id} className="hover:bg-gray-50">
                  <td className="py-3 px-4 font-medium text-gray-900">{source.name}</td>
                  <td className="py-3 px-4 text-gray-500 text-xs uppercase">{source.source_type}</td>
                  <td className="py-3 px-4 text-gray-500 text-xs font-mono">{source.schedule || "手动"}</td>
                  <td className="py-3 px-4 text-gray-400 text-xs">
                    {source.last_run_at ? new Date(source.last_run_at).toLocaleString("zh-CN") : "从未"}
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center justify-end gap-3">
                      <button
                        onClick={() => triggerRun(source.id)}
                        disabled={runningId === source.id}
                        className="text-blue-600 hover:text-blue-700 text-xs font-medium disabled:opacity-50"
                      >
                        {runningId === source.id ? "运行中..." : "立即采集"}
                      </button>
                      <fetcher.Form method="post" className="inline">
                        <input type="hidden" name="intent" value="delete_source" />
                        <input type="hidden" name="sourceId" value={source.id} />
                        <button
                          type="submit"
                          onClick={(e) => { if (!confirm("确认删除?")) e.preventDefault(); }}
                          className="text-red-500 hover:text-red-700 text-xs"
                        >
                          删除
                        </button>
                      </fetcher.Form>
                    </div>
                  </td>
                </tr>
              ))}
              {(sources || []).length === 0 && (
                <tr><td colSpan={5} className="py-12 text-center text-gray-400">暂无数据源</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Entries Tab */}
      {activeTab === "entries" && (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 bg-gray-50">
                <th className="text-left py-3 px-4 font-medium text-gray-600">标题</th>
                <th className="text-left py-3 px-4 font-medium text-gray-600">行业/平台</th>
                <th className="text-left py-3 px-4 font-medium text-gray-600">状态</th>
                <th className="text-left py-3 px-4 font-medium text-gray-600">时间</th>
                <th className="text-right py-3 px-4 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(entries?.items || []).map((entry) => {
                const statusInfo = STATUS_MAP[entry.status] || STATUS_MAP.pending;
                return (
                  <tr key={entry.id} className="hover:bg-gray-50">
                    <td className="py-3 px-4">
                      <div className="text-sm text-gray-900 line-clamp-1 max-w-xs">{entry.title}</div>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex gap-1">
                        {entry.industry && <span className="text-xs rounded px-1.5 py-0.5 bg-orange-50 text-orange-600">{entry.industry}</span>}
                        {entry.platform && <span className="text-xs rounded px-1.5 py-0.5 bg-purple-50 text-purple-600">{entry.platform}</span>}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${statusInfo.color}`}>
                        {statusInfo.label}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-gray-400 text-xs">
                      {new Date(entry.created_at).toLocaleDateString("zh-CN")}
                    </td>
                    <td className="py-3 px-4">
                      {entry.status === "pending" && (
                        <div className="flex items-center justify-end gap-2">
                          <fetcher.Form method="post" className="inline">
                            <input type="hidden" name="intent" value="approve" />
                            <input type="hidden" name="entryId" value={entry.id} />
                            <button type="submit" className="text-green-600 hover:text-green-700 text-xs font-medium">通过</button>
                          </fetcher.Form>
                          <fetcher.Form method="post" className="inline">
                            <input type="hidden" name="intent" value="reject" />
                            <input type="hidden" name="entryId" value={entry.id} />
                            <button type="submit" className="text-red-500 hover:text-red-700 text-xs">拒绝</button>
                          </fetcher.Form>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
              {(entries?.items || []).length === 0 && (
                <tr><td colSpan={5} className="py-12 text-center text-gray-400">暂无情报条目</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Create Source Modal */}
      {showCreateSource && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 p-6">
            <h3 className="text-base font-semibold text-gray-900 mb-4">添加数据源</h3>
            <fetcher.Form method="post" className="space-y-3" onSubmit={() => setShowCreateSource(false)}>
              <input type="hidden" name="intent" value="create_source" />
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">名称 *</label>
                <input name="name" required placeholder="例: 数字营销日报RSS" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">类型 *</label>
                  <select name="source_type" defaultValue="rss" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm">
                    <option value="rss">RSS</option>
                    <option value="crawler">网页爬取</option>
                    <option value="manual">手动</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">定时计划 (Cron)</label>
                  <input name="schedule" placeholder="0 8 * * *" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm font-mono" />
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">配置 (JSON)</label>
                <textarea name="config" rows={3} defaultValue='{"url": ""}' className="w-full rounded-lg border border-gray-200 px-3 py-2 text-xs font-mono" />
              </div>
              <div className="flex gap-3 pt-2">
                <button type="submit" className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">添加</button>
                <button type="button" onClick={() => setShowCreateSource(false)} className="rounded-lg border border-gray-200 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50">取消</button>
              </div>
            </fetcher.Form>
          </div>
        </div>
      )}
    </div>
  );
}
