import { Link, useLoaderData, useFetcher } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { Skill } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const skills = await apiFetch("/api/skills", { token });
  return { skills };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;
  const skillId = form.get("skillId") as string;

  if (intent === "publish") {
    await apiFetch(`/api/skills/${skillId}/status?status=published`, { method: "PATCH", token });
  } else if (intent === "archive") {
    await apiFetch(`/api/skills/${skillId}/status?status=archived`, { method: "PATCH", token });
  } else if (intent === "draft") {
    await apiFetch(`/api/skills/${skillId}/status?status=draft`, { method: "PATCH", token });
  }
  return null;
}

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  draft:     { label: "草稿",   color: "bg-gray-100 text-gray-600 border-gray-400" },
  reviewing: { label: "审核中", color: "bg-yellow-100 text-yellow-700 border-yellow-400" },
  published: { label: "已发布", color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
  archived:  { label: "已归档", color: "bg-red-100 text-red-600 border-red-400" },
};

const MODE_LABELS: Record<string, string> = {
  structured: "结构化",
  unstructured: "非结构化",
  hybrid: "混合",
};

export default function SkillList() {
  const { skills } = useLoaderData<typeof loader>() as { skills: Skill[] };
  const fetcher = useFetcher();

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">Skill 管理</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">管理 AI 工作流技能和 Prompt</p>
          </div>
        </div>
        <Link
          to="/admin/skills/new"
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
        >
          + 新建 Skill
        </Link>
      </div>

      <div className="p-6 max-w-6xl">
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Skill_Registry</span>
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
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">模式</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">版本</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">标签</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {skills.map((skill) => {
                const statusInfo = STATUS_LABELS[skill.status] || { label: skill.status, color: "bg-gray-100 text-gray-600 border-gray-400" };
                return (
                  <tr key={skill.id} className="hover:bg-[#F0F4F8] transition-colors">
                    <td className="py-3 px-4">
                      <div className="text-xs font-bold text-[#1A202C]">{skill.name}</div>
                      <div className="text-[9px] text-gray-400 mt-0.5 truncate max-w-48 uppercase">{skill.description}</div>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold uppercase text-gray-500">
                      {MODE_LABELS[skill.mode] || skill.mode}
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-block border px-2 py-0.5 text-[9px] font-bold uppercase ${statusInfo.color}`}>
                        {statusInfo.label}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold text-gray-500 uppercase">v{skill.current_version}</td>
                    <td className="py-3 px-4">
                      <div className="flex flex-wrap gap-1">
                        {(skill.knowledge_tags || []).slice(0, 3).map((tag) => (
                          <span key={tag} className="px-1.5 py-0.5 text-[9px] font-bold uppercase bg-[#CCF2FF] border border-[#00D1FF] text-[#00A3C4]">
                            {tag}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center justify-end gap-3">
                        <Link
                          to={`/admin/skills/${skill.id}`}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          编辑
                        </Link>
                        <fetcher.Form method="post">
                          <input type="hidden" name="skillId" value={skill.id} />
                          {skill.status === "published" ? (
                            <button name="intent" value="archive" className="text-[10px] font-bold uppercase text-gray-500 hover:text-[#1A202C]">
                              归档
                            </button>
                          ) : skill.status !== "archived" ? (
                            <button name="intent" value="publish" className="text-[10px] font-bold uppercase text-green-600 hover:text-green-800">
                              发布
                            </button>
                          ) : (
                            <button name="intent" value="draft" className="text-[10px] font-bold uppercase text-gray-500 hover:text-[#1A202C]">
                              恢复草稿
                            </button>
                          )}
                        </fetcher.Form>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {skills.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-xs font-bold uppercase text-gray-400">
                    暂无 Skill — 点击右上角新建
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
