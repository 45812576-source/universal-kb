import { useState } from "react";
import { useFetcher, useLoaderData, useRevalidator } from "react-router";
import type { Route } from "./+types/approvals";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

// ─── Types ────────────────────────────────────────────────────────────────────

interface ApprovalAction {
  id: number;
  actor_id: number;
  actor_name: string | null;
  action: "approve" | "reject" | "add_conditions";
  comment: string | null;
  created_at: string;
}

interface ApprovalRequest {
  id: number;
  request_type: string;
  target_id: number | null;
  target_type: string | null;
  requester_id: number;
  requester_name: string | null;
  status: "pending" | "approved" | "rejected" | "published_with_conditions";
  conditions: string[];
  created_at: string;
  actions: ApprovalAction[];
}

// ─── Loader ───────────────────────────────────────────────────────────────────

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const url = new URL(request.url);
  const status = url.searchParams.get("status") || "";
  const type = url.searchParams.get("type") || "";
  const page = url.searchParams.get("page") || "1";

  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (type) params.set("type", type);
  params.set("page", page);
  params.set("page_size", "20");

  const data = await apiFetch(`/api/approvals?${params}`, { token });
  return { ...data, currentUser: user, token };
}

// ─── Action ───────────────────────────────────────────────────────────────────

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const body = await request.json();
  const { intent, request_id, ...rest } = body as {
    intent: string;
    request_id: number;
    [k: string]: unknown;
  };

  if (intent === "act") {
    await apiFetch(`/api/approvals/${request_id}/actions`, {
      method: "POST",
      body: JSON.stringify(rest),
      token,
    });
  }
  return null;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const STATUS_MAP: Record<string, { label: string; color: string }> = {
  pending:                    { label: "待审批", color: "bg-yellow-100 text-yellow-700 border-yellow-400" },
  approved:                   { label: "已批准", color: "bg-green-100 text-green-700 border-green-400" },
  rejected:                   { label: "已拒绝", color: "bg-red-100 text-red-600 border-red-400" },
  published_with_conditions:  { label: "附条件通过", color: "bg-blue-100 text-blue-700 border-blue-400" },
};

const TYPE_MAP: Record<string, string> = {
  skill_publish:       "Skill 发布",
  skill_policy_change: "策略变更",
  data_scope_expand:   "范围扩大",
  output_schema:       "输出 Schema",
};

const ACTION_MAP: Record<string, string> = {
  approve:        "批准",
  reject:         "拒绝",
  add_conditions: "附条件通过",
};

function fmt(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// ─── Approval Detail Modal ────────────────────────────────────────────────────

function ApprovalModal({
  req,
  isAdmin,
  onClose,
}: {
  req: ApprovalRequest;
  isAdmin: boolean;
  onClose: () => void;
}) {
  const fetcher = useFetcher();
  const [comment, setComment] = useState("");
  const isPending = req.status === "pending";
  const isSubmitting = fetcher.state !== "idle";

  function act(action: string) {
    fetcher.submit(
      { intent: "act", request_id: req.id, action, comment: comment || null },
      { method: "POST", encType: "application/json" },
    );
  }

  const statusInfo = STATUS_MAP[req.status] || { label: req.status, color: "bg-gray-100 text-gray-600 border-gray-400" };

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white pixel-border w-full max-w-lg flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C] flex-shrink-0">
          <span className="text-[10px] font-bold uppercase tracking-widest">审批详情 #{req.id}</span>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-xs font-bold">✕</button>
        </div>

        <div className="overflow-y-auto flex-1 p-4 space-y-4">
          {/* Meta */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-0.5">类型</div>
              <div className="text-xs font-bold text-[#1A202C]">{TYPE_MAP[req.request_type] || req.request_type}</div>
            </div>
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-0.5">状态</div>
              <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${statusInfo.color}`}>
                {statusInfo.label}
              </span>
            </div>
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-0.5">申请人</div>
              <div className="text-xs text-[#1A202C]">{req.requester_name || `#${req.requester_id}`}</div>
            </div>
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-0.5">申请时间</div>
              <div className="text-xs text-[#1A202C]">{fmt(req.created_at)}</div>
            </div>
            {req.target_type && (
              <div className="col-span-2">
                <div className="text-[9px] font-bold uppercase text-gray-400 mb-0.5">对象</div>
                <div className="text-xs text-[#1A202C]">{req.target_type} #{req.target_id}</div>
              </div>
            )}
          </div>

          {/* Conditions */}
          {req.conditions.length > 0 && (
            <div className="border-2 border-blue-200 bg-blue-50 p-3">
              <div className="text-[9px] font-bold uppercase text-blue-600 mb-1.5">附加条件</div>
              <ul className="space-y-1">
                {req.conditions.map((c, i) => (
                  <li key={i} className="text-xs text-blue-800 flex gap-2">
                    <span className="text-blue-400 flex-shrink-0">›</span>
                    <span>{c}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Timeline */}
          {req.actions.length > 0 && (
            <div>
              <div className="text-[9px] font-bold uppercase text-gray-400 mb-2">审批记录</div>
              <div className="space-y-2">
                {req.actions.map((a) => (
                  <div key={a.id} className="flex gap-2.5 text-xs">
                    <div className="flex-shrink-0 w-1.5 h-1.5 mt-1.5 bg-[#00A3C4]" />
                    <div>
                      <span className="font-bold text-[#1A202C]">{a.actor_name}</span>
                      <span className="text-gray-400 mx-1">·</span>
                      <span className="font-bold text-[#00A3C4]">{ACTION_MAP[a.action] || a.action}</span>
                      <span className="text-gray-400 ml-2 text-[9px]">{fmt(a.created_at)}</span>
                      {a.comment && <div className="text-gray-500 mt-0.5 text-[10px]">{a.comment}</div>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Action area */}
          {isAdmin && isPending && (
            <div className="border-t-2 border-[#1A202C] pt-3 space-y-2">
              <div className="text-[9px] font-bold uppercase text-gray-400">审批意见（可选）</div>
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                rows={2}
                placeholder="输入审批说明..."
                className="w-full text-xs border-2 border-[#1A202C] px-2 py-1.5 resize-none focus:outline-none focus:border-[#00A3C4]"
              />
              <div className="flex gap-2">
                <button
                  onClick={() => act("approve")}
                  disabled={isSubmitting}
                  className="flex-1 bg-green-600 text-white px-3 py-1.5 text-[10px] font-bold uppercase tracking-wide hover:bg-green-700 disabled:opacity-50"
                >
                  批准
                </button>
                <button
                  onClick={() => act("add_conditions")}
                  disabled={isSubmitting}
                  className="flex-1 bg-blue-600 text-white px-3 py-1.5 text-[10px] font-bold uppercase tracking-wide hover:bg-blue-700 disabled:opacity-50"
                >
                  附条件通过
                </button>
                <button
                  onClick={() => act("reject")}
                  disabled={isSubmitting}
                  className="flex-1 bg-red-600 text-white px-3 py-1.5 text-[10px] font-bold uppercase tracking-wide hover:bg-red-700 disabled:opacity-50"
                >
                  拒绝
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ApprovalsPage() {
  const { items, total, page, page_size, currentUser } = useLoaderData<typeof loader>() as {
    items: ApprovalRequest[];
    total: number;
    page: number;
    page_size: number;
    currentUser: { role: string };
  };

  const revalidator = useRevalidator();
  const [selected, setSelected] = useState<ApprovalRequest | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");

  const isAdmin = currentUser.role === "super_admin" || currentUser.role === "dept_admin";
  const totalPages = Math.ceil(total / page_size);

  function applyFilter() {
    const params = new URLSearchParams();
    if (statusFilter) params.set("status", statusFilter);
    if (typeFilter) params.set("type", typeFilter);
    window.location.href = `/admin/approvals?${params}`;
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">审批管理</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">Skill 发布 / 策略变更 / 权限扩大</p>
          </div>
        </div>
        <div className="text-[10px] text-gray-500 font-bold uppercase">共 {total} 条</div>
      </div>

      <div className="p-6 max-w-6xl space-y-4">
        {/* Filters */}
        <div className="flex gap-3 items-end">
          <div>
            <div className="text-[9px] font-bold uppercase text-gray-500 mb-1">状态</div>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none focus:border-[#00A3C4]"
            >
              <option value="">全部</option>
              <option value="pending">待审批</option>
              <option value="approved">已批准</option>
              <option value="rejected">已拒绝</option>
              <option value="published_with_conditions">附条件通过</option>
            </select>
          </div>
          <div>
            <div className="text-[9px] font-bold uppercase text-gray-500 mb-1">类型</div>
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="border-2 border-[#1A202C] px-2 py-1.5 text-xs font-bold bg-white focus:outline-none focus:border-[#00A3C4]"
            >
              <option value="">全部</option>
              {Object.entries(TYPE_MAP).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </select>
          </div>
          <button
            onClick={applyFilter}
            className="bg-[#1A202C] text-white px-4 py-1.5 text-[10px] font-bold uppercase tracking-widest hover:bg-black"
          >
            筛选
          </button>
        </div>

        {/* Table */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Approval_Queue</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          {items.length === 0 ? (
            <div className="py-16 text-center text-gray-400 text-xs font-bold uppercase">暂无审批申请</div>
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="border-b-2 border-[#1A202C] bg-[#F0F4F8]">
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">ID</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">类型</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">申请人</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">时间</th>
                  <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.map((req) => {
                  const statusInfo = STATUS_MAP[req.status] || { label: req.status, color: "bg-gray-100 text-gray-600 border-gray-400" };
                  return (
                    <tr key={req.id} className="hover:bg-[#F0F4F8] transition-colors">
                      <td className="py-3 px-4 text-xs font-bold text-gray-400">#{req.id}</td>
                      <td className="py-3 px-4">
                        <div className="text-xs font-bold text-[#1A202C]">{TYPE_MAP[req.request_type] || req.request_type}</div>
                        {req.target_type && (
                          <div className="text-[9px] text-gray-400 uppercase">{req.target_type} #{req.target_id}</div>
                        )}
                      </td>
                      <td className="py-3 px-4 text-xs text-[#1A202C]">
                        {req.requester_name || `#${req.requester_id}`}
                      </td>
                      <td className="py-3 px-4">
                        <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${statusInfo.color}`}>
                          {statusInfo.label}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-[10px] text-gray-500">{fmt(req.created_at)}</td>
                      <td className="py-3 px-4 text-right">
                        <button
                          onClick={() => setSelected(req)}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          详情
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center gap-2">
            {Array.from({ length: totalPages }, (_, i) => i + 1).map((p) => (
              <a
                key={p}
                href={`/admin/approvals?page=${p}${statusFilter ? `&status=${statusFilter}` : ""}${typeFilter ? `&type=${typeFilter}` : ""}`}
                className={`w-7 h-7 flex items-center justify-center text-[10px] font-bold border-2 ${
                  p === page
                    ? "bg-[#1A202C] text-white border-[#1A202C]"
                    : "border-[#1A202C] text-[#1A202C] hover:bg-[#CCF2FF]"
                }`}
              >
                {p}
              </a>
            ))}
          </div>
        )}
      </div>

      {/* Modal */}
      {selected && (
        <ApprovalModal
          req={selected}
          isAdmin={isAdmin}
          onClose={() => {
            setSelected(null);
            revalidator.revalidate();
          }}
        />
      )}
    </div>
  );
}
