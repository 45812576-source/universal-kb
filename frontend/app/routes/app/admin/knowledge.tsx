import { useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/knowledge";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { KnowledgeEntry } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const url = new URL(request.url);
  const reviewStage = url.searchParams.get("review_stage") || "";

  const params = new URLSearchParams({ status: "pending" });
  if (reviewStage) params.set("review_stage", reviewStage);

  const entries = await apiFetch(`/api/knowledge?${params}`, { token });

  // 超管额外获取 dept_approved_pending_super 条目
  let superPendingEntries: KnowledgeEntry[] = [];
  if (user.role === "super_admin") {
    superPendingEntries = await apiFetch(
      "/api/knowledge?review_stage=dept_approved_pending_super",
      { token }
    );
  }

  return { entries, superPendingEntries, userRole: user.role, reviewStage };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const actionType = form.get("action") as string;
  const id = form.get("id") as string;
  const note = form.get("note") as string;
  const isSuperReview = form.get("is_super_review") === "true";

  const endpoint = isSuperReview
    ? `/api/knowledge/${id}/super-review`
    : `/api/knowledge/${id}/review`;

  await apiFetch(endpoint, {
    method: "POST",
    body: JSON.stringify({ action: actionType, note }),
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

const REVIEW_LEVEL_COLORS: Record<number, string> = {
  1: "bg-green-100 border-green-400 text-green-700",
  2: "bg-yellow-100 border-yellow-400 text-yellow-700",
  3: "bg-red-100 border-red-400 text-red-700",
};

const STAGE_FILTER_OPTIONS = [
  { value: "", label: "全部待审核" },
  { value: "pending_dept", label: "待部门审核" },
  { value: "dept_approved_pending_super", label: "待超管确认 (L3)" },
];

export default function AdminKnowledge() {
  const { entries, superPendingEntries, userRole, reviewStage } =
    useLoaderData<typeof loader>() as {
      entries: KnowledgeEntry[];
      superPendingEntries: KnowledgeEntry[];
      userRole: string;
      reviewStage: string;
    };

  const fetcher = useFetcher();
  const [selected, setSelected] = useState<KnowledgeEntry | null>(null);
  const [note, setNote] = useState("");
  const isSubmitting = fetcher.state !== "idle";

  // 显示：普通待审 + 超管二次确认队列合并
  const allEntries = [
    ...entries,
    ...superPendingEntries.filter(
      (e) => !entries.find((x) => x.id === e.id)
    ),
  ];

  const handleAction = (action: "approve" | "reject", isSuperReview = false) => {
    if (!selected) return;
    const fd = new FormData();
    fd.set("action", action);
    fd.set("id", String(selected.id));
    fd.set("note", note);
    fd.set("is_super_review", String(isSuperReview));
    fetcher.submit(fd, { method: "post" });
    setSelected(null);
    setNote("");
  };

  const isSuperPending =
    selected?.review_stage === "dept_approved_pending_super";

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">知识审核</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">审核通过后自动向量化入库</p>
        </div>
        {/* 筛选器 */}
        {userRole === "super_admin" && (
          <div className="ml-auto flex items-center gap-2">
            {STAGE_FILTER_OPTIONS.map((opt) => (
              <a
                key={opt.value}
                href={opt.value ? `?review_stage=${opt.value}` : "?"}
                className={`text-[9px] font-bold uppercase px-2 py-1 border-2 transition-colors ${
                  reviewStage === opt.value
                    ? "bg-[#1A202C] text-[#00D1FF] border-[#1A202C]"
                    : "border-gray-300 text-gray-500 hover:border-[#1A202C]"
                }`}
              >
                {opt.label}
              </a>
            ))}
          </div>
        )}
      </div>

      <div className="p-6 flex gap-4 h-[calc(100vh-120px)]">
        {/* List */}
        <div className="w-72 flex-shrink-0 pixel-border bg-white flex flex-col overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              Pending_Queue [{allEntries.length}]
            </span>
          </div>
          <div className="flex-1 overflow-y-auto divide-y divide-gray-100">
            {allEntries.length === 0 && (
              <div className="py-12 text-center text-xs font-bold uppercase text-gray-400">
                暂无待审核内容
              </div>
            )}
            {allEntries.map((e) => (
              <button
                key={e.id}
                onClick={() => { setSelected(e); setNote(""); }}
                className={`w-full text-left px-4 py-3 transition-colors ${
                  selected?.id === e.id ? "bg-[#CCF2FF]" : "hover:bg-[#F0F4F8]"
                }`}
              >
                <div className="flex items-center gap-1.5 mb-1">
                  {/* 审核级别 badge */}
                  <span
                    className={`px-1 py-0.5 text-[8px] font-bold uppercase border ${
                      REVIEW_LEVEL_COLORS[e.review_level] || REVIEW_LEVEL_COLORS[2]
                    }`}
                  >
                    {e.review_level_label || `L${e.review_level}`}
                  </span>
                  {e.review_stage === "dept_approved_pending_super" && (
                    <span className="px-1 py-0.5 text-[8px] font-bold uppercase border border-purple-400 bg-purple-50 text-purple-700">
                      超管待确认
                    </span>
                  )}
                </div>
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
                {/* 审核级别 + 状态信息 */}
                <div className="flex items-center gap-2 mb-3">
                  <span
                    className={`px-2 py-0.5 text-[9px] font-bold uppercase border ${
                      REVIEW_LEVEL_COLORS[selected.review_level] || REVIEW_LEVEL_COLORS[2]
                    }`}
                  >
                    {selected.review_level_label}
                  </span>
                  <span className="px-2 py-0.5 text-[9px] font-bold uppercase border border-gray-300 text-gray-500">
                    {selected.review_stage_label}
                  </span>
                  <span className="px-2 py-0.5 text-[9px] font-bold uppercase border border-gray-200 text-gray-400">
                    {selected.capture_mode}
                  </span>
                </div>

                {/* 敏感词警告 */}
                {selected.sensitivity_flags && selected.sensitivity_flags.length > 0 && (
                  <div className="mb-3 border-2 border-orange-400 bg-orange-50 px-3 py-2">
                    <p className="text-[9px] font-bold uppercase text-orange-700 mb-1">
                      ⚠ 检测到敏感词
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {selected.sensitivity_flags.map((f) => (
                        <span
                          key={f}
                          className="px-1 py-0.5 text-[8px] font-bold bg-orange-100 border border-orange-300 text-orange-700"
                        >
                          {f}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                <h2 className="text-sm font-bold text-[#1A202C] mb-2">{selected.title}</h2>
                <div className="flex items-center gap-4 text-[9px] font-bold uppercase text-gray-400 mb-3">
                  <span>分类: {CATEGORY_LABELS[selected.category] || selected.category}</span>
                  <span>来源: {selected.source_type}</span>
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

                {selected.auto_review_note && (
                  <p className="mt-3 text-[9px] font-bold text-gray-400 uppercase">
                    AI审核说明: {selected.auto_review_note}
                  </p>
                )}
              </div>

              <div className="border-t-2 border-[#1A202C] p-4 space-y-3 bg-[#EBF4F7]">
                {isSuperPending && (
                  <div className="text-[9px] font-bold uppercase text-purple-700 bg-purple-50 border border-purple-300 px-2 py-1">
                    ★ 此条目已通过部门审核，需超管二次确认 (L3)
                  </div>
                )}
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
                    onClick={() => handleAction("approve", isSuperPending)}
                    disabled={isSubmitting}
                    className="flex-1 bg-[#1A202C] text-[#00D1FF] py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
                  >
                    {isSubmitting ? "处理中..." : isSuperPending ? "[OK] 超管确认通过" : "[OK] 通过"}
                  </button>
                  <button
                    onClick={() => handleAction("reject", isSuperPending)}
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
