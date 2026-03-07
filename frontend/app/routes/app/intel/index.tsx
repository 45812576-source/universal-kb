import { useState } from "react";
import { useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

interface IntelEntry {
  id: number;
  title: string;
  content: string;
  url: string | null;
  tags: string[];
  industry: string | null;
  platform: string | null;
  status: "pending" | "approved" | "rejected";
  created_at: string;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const url = new URL(request.url);
  const q = url.searchParams.get("q") || "";
  const industry = url.searchParams.get("industry") || "";
  const page = url.searchParams.get("page") || "1";

  const params = new URLSearchParams({ page, page_size: "20" });
  if (q) params.set("q", q);
  if (industry) params.set("industry", industry);

  const data = await apiFetch(`/api/intel/entries?${params}`, { token });
  return { data, token, user, q, industry };
}

export default function IntelIndex() {
  const { data, token, user, q: initialQ, industry: initialIndustry } = useLoaderData<typeof loader>() as {
    data: { total: number; items: IntelEntry[] };
    token: string;
    user: any;
    q: string;
    industry: string;
  };
  const [selectedEntry, setSelectedEntry] = useState<IntelEntry | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  async function loadDetail(entry: IntelEntry) {
    setLoadingDetail(true);
    try {
      const full = await apiFetch(`/api/intel/entries/${entry.id}`, { token });
      setSelectedEntry(full);
    } finally {
      setLoadingDetail(false);
    }
  }

  const isAdmin = user?.role === "super_admin" || user?.role === "dept_admin";

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">情报中心</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">平台政策、行业报告等外部情报</p>
        </div>
      </div>

      <div className="p-6 max-w-6xl">
        {/* Search */}
        <form className="flex gap-3 mb-6" method="get">
          <input
            name="q"
            defaultValue={initialQ}
            placeholder="搜索标题或内容..."
            className="flex-1 border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
          />
          <input
            name="industry"
            defaultValue={initialIndustry}
            placeholder="行业筛选"
            className="w-32 border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
          />
          <button
            type="submit"
            className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
          >
            搜索
          </button>
          {isAdmin && (
            <a
              href="/admin/intel"
              className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-[#EBF4F7] transition-colors"
            >
              管理数据源
            </a>
          )}
        </form>

        <div className="flex gap-6">
          {/* Entry List */}
          <div className="flex-1 space-y-3">
            {(data?.items || []).length === 0 ? (
              <div className="pixel-border bg-white p-12 text-center">
                <p className="text-xs font-bold uppercase text-gray-400">暂无情报数据</p>
              </div>
            ) : (
              (data?.items || []).map((entry) => (
                <div
                  key={entry.id}
                  onClick={() => loadDetail(entry)}
                  className={`pixel-border bg-white p-4 cursor-pointer transition-colors ${
                    selectedEntry?.id === entry.id ? "bg-[#CCF2FF]" : "hover:bg-[#EBF4F7]"
                  }`}
                >
                  <h3 className="text-xs font-bold text-[#1A202C] mb-1 line-clamp-2">{entry.title}</h3>
                  <p className="text-[10px] text-gray-500 line-clamp-2">{entry.content}</p>
                  <div className="flex items-center gap-2 mt-2 flex-wrap">
                    {entry.industry && (
                      <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-orange-50 border border-orange-300 text-orange-600">{entry.industry}</span>
                    )}
                    {entry.platform && (
                      <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-purple-50 border border-purple-300 text-purple-600">{entry.platform}</span>
                    )}
                    {(entry.tags || []).slice(0, 3).map((tag) => (
                      <span key={tag} className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-[#EBF4F7] border border-gray-300 text-gray-600">{tag}</span>
                    ))}
                    <span className="ml-auto text-[9px] font-bold uppercase text-gray-400">
                      {new Date(entry.created_at).toLocaleDateString("zh-CN")}
                    </span>
                  </div>
                </div>
              ))
            )}
          </div>

          {/* Detail Panel */}
          {selectedEntry && (
            <div className="w-96 flex-shrink-0 pixel-border bg-white p-5 h-fit sticky top-6">
              {loadingDetail ? (
                <p className="text-xs font-bold uppercase text-gray-400">加载中...</p>
              ) : (
                <>
                  <div className="flex items-start justify-between mb-3 gap-3">
                    <h2 className="text-xs font-bold text-[#1A202C] flex-1">{selectedEntry.title}</h2>
                    <button
                      onClick={() => setSelectedEntry(null)}
                      className="text-gray-400 hover:text-[#1A202C] flex-shrink-0 font-bold text-xs"
                    >
                      [X]
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1 mb-3">
                    {selectedEntry.industry && (
                      <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-orange-50 border border-orange-300 text-orange-600">{selectedEntry.industry}</span>
                    )}
                    {selectedEntry.platform && (
                      <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-purple-50 border border-purple-300 text-purple-600">{selectedEntry.platform}</span>
                    )}
                    {(selectedEntry.tags || []).map((tag) => (
                      <span key={tag} className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-[#EBF4F7] border border-gray-300 text-gray-600">{tag}</span>
                    ))}
                  </div>
                  <div className="text-xs font-bold text-[#1A202C] whitespace-pre-wrap leading-relaxed max-h-96 overflow-y-auto border-2 border-[#1A202C] bg-[#F0F4F8] p-3">
                    {selectedEntry.content}
                  </div>
                  {selectedEntry.url && (
                    <a
                      href={selectedEntry.url}
                      target="_blank"
                      rel="noreferrer"
                      className="block mt-3 text-[10px] font-bold uppercase text-[#00A3C4] hover:underline truncate"
                    >
                      查看原文 &gt;
                    </a>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
