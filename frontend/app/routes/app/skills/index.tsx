import { useState } from "react";
import { Link, useLoaderData, useFetcher } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Skill, User } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);

  const [mySkills, deptSkills, companySkills, marketSkills] = await Promise.all([
    apiFetch("/api/skills?mine=true", { token }),
    apiFetch("/api/skills?scope=department", { token }),
    apiFetch("/api/skills?scope=company", { token }),
    apiFetch("/api/skill-market/search?limit=20", { token }).catch(() => []),
  ]);

  return { mySkills, deptSkills, companySkills, marketSkills, user };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;
  const skillId = form.get("skillId") as string;

  if (intent === "delete") {
    await apiFetch(`/api/skills/${skillId}`, { method: "DELETE", token });
  }
  return null;
}

const STATUS_INFO: Record<string, { label: string; color: string }> = {
  draft:     { label: "草稿",   color: "bg-gray-100 text-gray-600 border-gray-400" },
  reviewing: { label: "审核中", color: "bg-yellow-100 text-yellow-800 border-yellow-400" },
  published: { label: "已发布", color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
  archived:  { label: "已归档", color: "bg-gray-100 text-gray-500 border-gray-300" },
};

function SkillCard({ skill, showActions, user }: { skill: Skill & { scope?: string; created_by?: number }; showActions?: boolean; user: User }) {
  const si = STATUS_INFO[skill.status] || { label: skill.status, color: "bg-gray-100 text-gray-500 border-gray-400" };
  const isOwner = skill.created_by === user.id;
  const canEdit = user.role === "super_admin" || user.role === "dept_admin";
  const fetcher = useFetcher();
  const isDeleting = fetcher.state !== "idle" && fetcher.formData?.get("intent") === "delete";

  if (isDeleting) return null;

  return (
    <div className="pixel-border bg-white p-4 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="text-xs font-bold text-[#1A202C] truncate">{skill.name}</div>
          {skill.description && (
            <p className="text-[10px] text-gray-500 mt-0.5 line-clamp-2">{skill.description}</p>
          )}
        </div>
        <span className={`flex-shrink-0 inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${si.color}`}>
          {si.label}
        </span>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        {(skill.knowledge_tags || []).slice(0, 3).map((tag) => (
          <span key={tag} className="text-[9px] font-bold uppercase px-1.5 py-0.5 bg-[#EBF4F7] border border-gray-300 text-gray-600">
            {tag}
          </span>
        ))}
        <span className="ml-auto flex items-center gap-2">
          {(skill as any).usage_count > 0 && (
            <span className="text-[9px] font-bold text-gray-400">{(skill as any).usage_count} 次调用</span>
          )}
          <span className="text-[9px] font-bold uppercase text-gray-400">
            v{skill.current_version}
          </span>
        </span>
      </div>

      {showActions && canEdit && (
        <div className="flex gap-2 pt-1 border-t border-gray-100 items-center">
          <Link
            to={`/admin/skills/${skill.id}`}
            className="text-[9px] font-bold uppercase text-[#00A3C4] hover:underline"
          >
            编辑 &gt;
          </Link>
          {isOwner && (
            <span className="text-[9px] font-bold uppercase text-gray-400">
              · 我创建
            </span>
          )}
          <fetcher.Form method="post" className="ml-auto">
            <input type="hidden" name="skillId" value={skill.id} />
            <button
              name="intent"
              value="delete"
              onClick={(e) => { if (!confirm(`确认删除 Skill「${skill.name}」？此操作不可恢复。`)) e.preventDefault(); }}
              className="text-[9px] font-bold uppercase text-red-500 hover:text-red-700"
            >
              删除
            </button>
          </fetcher.Form>
        </div>
      )}
    </div>
  );
}

type Tab = "my" | "department" | "company" | "market";

export default function SkillsIndex() {
  const { mySkills, deptSkills, companySkills, marketSkills, user } = useLoaderData<typeof loader>() as {
    mySkills: (Skill & { scope?: string; created_by?: number })[];
    deptSkills: (Skill & { scope?: string; created_by?: number })[];
    companySkills: (Skill & { scope?: string; created_by?: number })[];
    marketSkills: any[];
    user: User;
  };

  const [tab, setTab] = useState<Tab>("my");
  const canEdit = user.role === "super_admin" || user.role === "dept_admin";

  const TABS: { key: Tab; label: string; count: number }[] = [
    { key: "my",         label: "我的 Skill",   count: mySkills.length },
    { key: "department", label: "部门 Skill",   count: deptSkills.length },
    { key: "company",    label: "公司通用",      count: companySkills.length },
    { key: "market",     label: "Skill Market", count: marketSkills.length },
  ];

  const currentItems = tab === "my" ? mySkills : tab === "department" ? deptSkills : tab === "company" ? companySkills : marketSkills;

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">Skill 中心</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">管理和浏览 AI 工作流模板</p>
          </div>
        </div>
        {canEdit && (
          <Link
            to="/admin/skills"
            className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
          >
            + 创建 Skill
          </Link>
        )}
      </div>

      <div className="p-6 max-w-5xl">
        {/* Tabs */}
        <div className="flex border-2 border-[#1A202C] w-fit mb-6">
          {TABS.map((t, i) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`px-4 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors ${
                i > 0 ? "border-l-2 border-[#1A202C]" : ""
              } ${
                tab === t.key
                  ? "bg-[#1A202C] text-white"
                  : "bg-white text-gray-600 hover:bg-gray-100"
              }`}
            >
              {t.label}
              {t.count > 0 && (
                <span className={`ml-1.5 text-[8px] font-bold px-1 py-0.5 ${tab === t.key ? "bg-white/20 text-white" : "bg-[#EBF4F7] text-gray-600"}`}>
                  {t.count}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Content */}
        {tab === "market" ? (
          <MarketTab items={marketSkills} />
        ) : (
          <div>
            {currentItems.length === 0 ? (
              <div className="pixel-border bg-white p-12 text-center">
                <p className="text-xs font-bold uppercase text-gray-400">
                  {tab === "my" ? "你还没有创建过 Skill" : "暂无数据"}
                </p>
                {tab === "my" && canEdit && (
                  <Link
                    to="/admin/skills"
                    className="mt-3 inline-block text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                  >
                    立即创建 &gt;
                  </Link>
                )}
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {(currentItems as (Skill & { scope?: string; created_by?: number })[]).map((s) => (
                  <SkillCard
                    key={s.id}
                    skill={s}
                    showActions={tab === "my"}
                    user={user}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function MarketTab({ items }: { items: any[] }) {
  return (
    <div>
      {items.length === 0 ? (
        <div className="pixel-border bg-white p-12 text-center">
          <p className="text-xs font-bold uppercase text-gray-400">暂无市场 Skill</p>
          <p className="text-[10px] text-gray-400 mt-1 uppercase">请联系管理员在「外部市场」中添加</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {items.map((item: any) => (
            <div key={item.id} className="pixel-border bg-white p-4">
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-bold text-[#1A202C] truncate">{item.name}</div>
                  {item.description && (
                    <p className="text-[10px] text-gray-500 mt-0.5 line-clamp-2">{item.description}</p>
                  )}
                </div>
                {item.upstream_version && (
                  <span className="flex-shrink-0 text-[9px] font-bold uppercase text-gray-400 border border-gray-300 px-1.5 py-0.5">
                    v{item.upstream_version}
                  </span>
                )}
              </div>
              {item.author && (
                <p className="mt-2 text-[9px] text-gray-400 uppercase font-bold">by {item.author}</p>
              )}
              <div className="mt-2 pt-2 border-t border-gray-100 flex gap-3">
                <span className="text-[9px] font-bold uppercase text-gray-400">
                  {item.download_count ?? 0} 次引用
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
