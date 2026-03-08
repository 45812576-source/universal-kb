import { Link, useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";

interface BusinessTable {
  id: number;
  table_name: string;
  display_name: string;
  description: string;
  columns: { name: string; type: string }[];
  created_at: string;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const tables = await apiFetch("/api/business-tables", { token });
  return { tables, token };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;
  if (intent === "delete") {
    const id = form.get("id") as string;
    try {
      await apiFetch(`/api/business-tables/${id}`, { method: "DELETE", token });
      return { success: "删除成功" };
    } catch (e) {
      if (e instanceof ApiError) return { error: e.message };
      return { error: "删除失败" };
    }
  }
  return null;
}

export default function BusinessTablesIndex() {
  const { tables } = useLoaderData<typeof loader>() as { tables: BusinessTable[] };

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">业务数据表管理</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">注册和管理业务数据表，支持自然语言生成</p>
          </div>
        </div>
        <Link
          to="/admin/business-tables/generate"
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
        >
          + 生成新数据表
        </Link>
      </div>

      <div className="p-6 max-w-5xl">
        {tables.length === 0 ? (
          <div className="border-2 border-dashed border-[#1A202C] p-12 text-center bg-white">
            <p className="text-xs font-bold uppercase text-gray-400 mb-2">暂无业务数据表</p>
            <Link to="/admin/business-tables/generate" className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">
              通过自然语言描述生成第一张表 &gt;
            </Link>
          </div>
        ) : (
          <div className="pixel-border bg-white overflow-hidden">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
              <span className="text-[10px] font-bold uppercase tracking-widest">Table_Registry</span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <table className="w-full text-left">
              <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
                <tr>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">表名</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">显示名</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">描述</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">列数</th>
                  <th className="px-4 py-3 text-[9px] font-bold uppercase tracking-widest text-gray-500">创建时间</th>
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {tables.map((t) => (
                  <tr key={t.id} className="hover:bg-[#F0F4F8]">
                    <td className="px-4 py-3 text-[10px] font-bold font-mono text-gray-600 uppercase">{t.table_name}</td>
                    <td className="px-4 py-3 text-xs font-bold text-[#1A202C]">{t.display_name}</td>
                    <td className="px-4 py-3 text-[10px] text-gray-500 max-w-xs truncate">{t.description}</td>
                    <td className="px-4 py-3 text-[10px] font-bold uppercase text-gray-400">{t.columns?.length ?? 0}</td>
                    <td className="px-4 py-3 text-[9px] font-bold uppercase text-gray-400">
                      {t.created_at ? new Date(t.created_at).toLocaleDateString("zh-CN") : "—"}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-3">
                        <Link to={`/data/${t.table_name}`} className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">
                          查看数据
                        </Link>
                        <form method="post" className="inline">
                          <input type="hidden" name="intent" value="delete" />
                          <input type="hidden" name="id" value={t.id} />
                          <button
                            type="submit"
                            onClick={(e) => { if (!confirm("确认删除注册信息？（不会删除实际数据表）")) e.preventDefault(); }}
                            className="text-[10px] font-bold uppercase text-red-500 hover:underline"
                          >
                            取消注册
                          </button>
                        </form>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
