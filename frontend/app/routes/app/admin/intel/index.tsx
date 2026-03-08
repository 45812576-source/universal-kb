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
  pending:  { label: "待审核", color: "bg-yellow-100 text-yellow-700 border-yellow-400" },
  approved: { label: "已通过", color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
  rejected: { label: "已拒绝", color: "bg-red-100 text-red-700 border-red-400" },
};

const TYPE_LABELS: Record<string, string> = {
  rss: "RSS",
  crawler: "爬虫",
  webhook: "Webhook",
  manual: "手动",
};

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">{children}</label>;
}

function PixelInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${props.className || ""}`} />;
}

function PixelSelect({ children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select {...props} className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]">
      {children}
    </select>
  );
}

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
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">情报管理</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">管理数据源和审核情报条目</p>
          </div>
        </div>
        {activeTab === "sources" && (
          <button
            onClick={() => setShowCreateSource(true)}
            className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
          >
            + 添加数据源
          </button>
        )}
      </div>

      <div className="p-6 max-w-6xl">
        {/* Tabs */}
        <div className="flex gap-0 mb-4 border-2 border-[#1A202C] w-fit">
          {(["sources", "entries"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors ${
                activeTab === tab
                  ? "bg-[#1A202C] text-white"
                  : "bg-white text-gray-500 hover:bg-[#EBF4F7]"
              }`}
            >
              {tab === "sources" ? "数据源管理" : `情报审核 (${entries?.total || 0})`}
            </button>
          ))}
        </div>

        {/* Sources Tab */}
        {activeTab === "sources" && (
          <div className="pixel-border bg-white overflow-hidden">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
              <span className="text-[10px] font-bold uppercase tracking-widest">Intel_Source_Registry</span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <table className="w-full text-left">
              <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
                <tr>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">名称</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">类型</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">计划</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">最后运行</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {(sources || []).map((source) => (
                  <tr key={source.id} className="hover:bg-[#F0F4F8]">
                    <td className="py-3 px-4 text-xs font-bold text-[#1A202C]">{source.name}</td>
                    <td className="py-3 px-4">
                      <span className="inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase bg-[#EBF4F7] text-[#1A202C] border-[#1A202C]">
                        {TYPE_LABELS[source.source_type] || source.source_type}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold font-mono text-gray-500">{source.schedule || "手动"}</td>
                    <td className="py-3 px-4 text-[9px] font-bold text-gray-400">
                      {source.last_run_at ? new Date(source.last_run_at).toLocaleString("zh-CN") : "从未"}
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center justify-end gap-3">
                        <button
                          onClick={() => triggerRun(source.id)}
                          disabled={runningId === source.id}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline disabled:opacity-50"
                        >
                          {runningId === source.id ? "运行中..." : "立即采集"}
                        </button>
                        <fetcher.Form method="post" className="inline">
                          <input type="hidden" name="intent" value="delete_source" />
                          <input type="hidden" name="sourceId" value={source.id} />
                          <button
                            type="submit"
                            onClick={(e) => { if (!confirm("确认删除?")) e.preventDefault(); }}
                            className="text-[10px] font-bold uppercase text-red-500 hover:underline"
                          >
                            删除
                          </button>
                        </fetcher.Form>
                      </div>
                    </td>
                  </tr>
                ))}
                {(sources || []).length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-12 text-center text-xs font-bold uppercase text-gray-400">暂无数据源 — 点击右上角添加</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {/* Entries Tab */}
        {activeTab === "entries" && (
          <div className="pixel-border bg-white overflow-hidden">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
              <span className="text-[10px] font-bold uppercase tracking-widest">Intel_Entry_Review</span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <table className="w-full text-left">
              <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
                <tr>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">标题</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">行业/平台</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">时间</th>
                  <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {(entries?.items || []).map((entry) => {
                  const statusInfo = STATUS_MAP[entry.status] || STATUS_MAP.pending;
                  return (
                    <tr key={entry.id} className="hover:bg-[#F0F4F8]">
                      <td className="py-3 px-4">
                        <div className="text-xs font-bold text-[#1A202C] max-w-xs truncate">{entry.title}</div>
                      </td>
                      <td className="py-3 px-4">
                        <div className="flex gap-1">
                          {entry.industry && (
                            <span className="inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase bg-orange-50 text-orange-700 border-orange-300">
                              {entry.industry}
                            </span>
                          )}
                          {entry.platform && (
                            <span className="inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase bg-purple-50 text-purple-700 border-purple-300">
                              {entry.platform}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="py-3 px-4">
                        <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${statusInfo.color}`}>
                          {statusInfo.label}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-[9px] font-bold text-gray-400">
                        {new Date(entry.created_at).toLocaleDateString("zh-CN")}
                      </td>
                      <td className="py-3 px-4">
                        {entry.status === "pending" && (
                          <div className="flex items-center justify-end gap-3">
                            <fetcher.Form method="post" className="inline">
                              <input type="hidden" name="intent" value="approve" />
                              <input type="hidden" name="entryId" value={entry.id} />
                              <button type="submit" className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">通过</button>
                            </fetcher.Form>
                            <fetcher.Form method="post" className="inline">
                              <input type="hidden" name="intent" value="reject" />
                              <input type="hidden" name="entryId" value={entry.id} />
                              <button type="submit" className="text-[10px] font-bold uppercase text-red-500 hover:underline">拒绝</button>
                            </fetcher.Form>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {(entries?.items || []).length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-12 text-center text-xs font-bold uppercase text-gray-400">暂无情报条目</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Create Source Modal */}
      {showCreateSource && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="pixel-border bg-white w-full max-w-lg mx-4">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-widest">添加数据源</span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <fetcher.Form method="post" className="p-6 space-y-3" onSubmit={() => setShowCreateSource(false)}>
              <input type="hidden" name="intent" value="create_source" />
              <div>
                <FieldLabel>名称 <span className="text-[#00D1FF]">*</span></FieldLabel>
                <PixelInput name="name" required placeholder="例: 数字营销日报RSS" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <FieldLabel>类型 <span className="text-[#00D1FF]">*</span></FieldLabel>
                  <PixelSelect name="source_type" defaultValue="rss">
                    <option value="rss">RSS</option>
                    <option value="crawler">网页爬取</option>
                    <option value="manual">手动</option>
                  </PixelSelect>
                </div>
                <div>
                  <FieldLabel>定时计划 (Cron)</FieldLabel>
                  <PixelInput name="schedule" placeholder="0 8 * * *" className="font-mono" />
                </div>
              </div>
              <div>
                <FieldLabel>配置 (JSON)</FieldLabel>
                <textarea
                  name="config"
                  rows={3}
                  defaultValue='{"url": ""}'
                  className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-mono font-bold focus:outline-none focus:border-[#00D1FF]"
                />
              </div>
              <div className="flex gap-3 pt-2">
                <button type="submit" className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border">添加</button>
                <button type="button" onClick={() => setShowCreateSource(false)} className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100">取消</button>
              </div>
            </fetcher.Form>
          </div>
        </div>
      )}
    </div>
  );
}
