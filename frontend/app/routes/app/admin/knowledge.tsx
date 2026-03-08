import { useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/knowledge";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { KnowledgeEntry } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const entries = await apiFetch("/api/knowledge?status=pending", { token });
  return { entries };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const action = form.get("action") as string;
  const id = form.get("id") as string;
  const note = form.get("note") as string;
  await apiFetch(`/api/knowledge/${id}/review`, {
    method: "POST",
    body: JSON.stringify({ action, note }),
    token,
  });
  return null;
}

const CATEGORY_LABELS: Record<string, string> = {
  experience: "经验总结",
  methodology: "方法论",
  case_study: "案例",
  data: "数据资产",
  template: "模板",
  external: "外部资料",
};

export default function AdminKnowledge() {
  const { entries } = useLoaderData<typeof loader>() as { entries: KnowledgeEntry[] };
  const fetcher = useFetcher();
  const [selected, setSelected] = useState<KnowledgeEntry | null>(null);
  const [note, setNote] = useState("");
  const isSubmitting = fetcher.state !== "idle";

  const handleAction = (action: "approve" | "reject") => {
    if (!selected) return;
    const fd = new FormData();
    fd.set("action", action);
    fd.set("id", String(selected.id));
    fd.set("note", note);
    fetcher.submit(fd, { method: "post" });
    setSelected(null);
    setNote("");
  };

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">知识审核</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">审核通过后自动向量化入库</p>
        </div>
      </div>

      <div className="p-6 flex gap-4 h-[calc(100vh-120px)]">
        {/* List */}
        <div className="w-72 flex-shrink-0 pixel-border bg-white flex flex-col overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              Pending_Queue [{entries.length}]
            </span>
          </div>
          <div className="flex-1 overflow-y-auto divide-y divide-gray-100">
            {entries.length === 0 && (
              <div className="py-12 text-center text-xs font-bold uppercase text-gray-400">
                暂无待审核内容
              </div>
            )}
            {entries.map((e) => (
              <button
                key={e.id}
                onClick={() => { setSelected(e); setNote(""); }}
                className={`w-full text-left px-4 py-3 transition-colors ${
                  selected?.id === e.id ? "bg-[#CCF2FF]" : "hover:bg-[#F0F4F8]"
                }`}
              >
                <div className="text-xs font-bold text-[#1A202C] truncate">{e.title}</div>
                <div className="text-[9px] text-gray-400 mt-0.5 flex items-center gap-2 uppercase font-bold">
                  <span>{CATEGORY_LABELS[e.category] || e.category}</span>
                  <span>·</span>
                  <span>{new Date(e.created_at).toLocaleDateString("zh-CN")}</span>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {[...e.industry_tags, ...e.topic_tags].slice(0, 3).map((tag) => (
                    <span key={tag} className="px-1 py-0.5 text-[9px] font-bold uppercase bg-[#EBF4F7] border border-gray-300 text-gray-500">
                      {tag}
                    </span>
                  ))}
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* Detail */}
        <div className="flex-1 pixel-border bg-white flex flex-col overflow-hidden">
          {!selected ? (
            <div className="flex-1 flex items-center justify-center">
              <p className="text-xs font-bold uppercase text-gray-400">&gt; 选择左侧条目查看详情</p>
            </div>
          ) : (
            <>
              <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
                <span className="text-[10px] font-bold uppercase tracking-widest">Entry_Detail</span>
                <div className="flex space-x-1.5">
                  <div className="w-2 h-2 bg-red-400" />
                  <div className="w-2 h-2 bg-yellow-400" />
                  <div className="w-2 h-2 bg-green-400" />
                </div>
              </div>

              <div className="flex-1 overflow-y-auto p-5">
                <h2 className="text-sm font-bold text-[#1A202C] mb-2">{selected.title}</h2>
                <div className="flex items-center gap-4 text-[9px] font-bold uppercase text-gray-400 mb-3">
                  <span>分类: {CATEGORY_LABELS[selected.category] || selected.category}</span>
                  <span>来源: {selected.source_type === "manual" ? "手动录入" : "文件上传"}</span>
                  <span>{new Date(selected.created_at).toLocaleString("zh-CN")}</span>
                </div>

                <div className="flex flex-wrap gap-1.5 mb-4">
                  {selected.industry_tags.map((t) => (
                    <span key={t} className="px-2 py-0.5 text-[9px] font-bold uppercase bg-[#CCF2FF] border border-[#00D1FF] text-[#00A3C4]">
                      行业: {t}
                    </span>
                  ))}
                  {selected.platform_tags.map((t) => (
                    <span key={t} className="px-2 py-0.5 text-[9px] font-bold uppercase bg-purple-50 border border-purple-300 text-purple-600">
                      平台: {t}
                    </span>
                  ))}
                  {selected.topic_tags.map((t) => (
                    <span key={t} className="px-2 py-0.5 text-[9px] font-bold uppercase bg-green-50 border border-green-300 text-green-700">
                      主题: {t}
                    </span>
                  ))}
                </div>

                <div className="border-2 border-[#1A202C] bg-[#F0F4F8] p-4 text-xs font-bold text-[#1A202C] whitespace-pre-wrap leading-relaxed">
                  {selected.content}
                </div>
              </div>

              <div className="border-t-2 border-[#1A202C] p-4 space-y-3 bg-[#EBF4F7]">
                <div>
                  <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
                    审核备注（可选）
                  </label>
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
                    placeholder="如有问题请填写原因..."
                  />
                </div>
                <div className="flex gap-3">
                  <button
                    onClick={() => handleAction("approve")}
                    disabled={isSubmitting}
                    className="flex-1 bg-[#1A202C] text-[#00D1FF] py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
                  >
                    {isSubmitting ? "处理中..." : "[OK] 通过"}
                  </button>
                  <button
                    onClick={() => handleAction("reject")}
                    disabled={isSubmitting}
                    className="flex-1 border-2 border-red-400 bg-red-50 py-2 text-[10px] font-bold uppercase tracking-widest text-red-600 hover:bg-red-100 disabled:opacity-50 transition-colors"
                  >
                    [X] 拒绝
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
