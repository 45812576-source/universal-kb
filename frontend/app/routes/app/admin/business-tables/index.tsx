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
    <div className="p-6 max-w-5xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-gray-900">业务数据表管理</h1>
          <p className="text-sm text-gray-500 mt-0.5">注册和管理业务数据表，支持自然语言生成</p>
        </div>
        <Link
          to="/admin/business-tables/generate"
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 transition-colors"
        >
          + 生成新数据表
        </Link>
      </div>

      {tables.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 p-12 text-center">
          <p className="text-gray-400 text-sm mb-2">暂无业务数据表</p>
          <Link
            to="/admin/business-tables/generate"
            className="text-sm text-blue-500 hover:text-blue-700"
          >
            通过自然语言描述生成第一张表 →
          </Link>
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase">表名</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase">显示名</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase">描述</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase">列数</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase">创建时间</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {tables.map((t) => (
                <tr key={t.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">{t.table_name}</td>
                  <td className="px-4 py-3 font-medium text-gray-900">{t.display_name}</td>
                  <td className="px-4 py-3 text-gray-500 max-w-xs truncate">{t.description}</td>
                  <td className="px-4 py-3 text-gray-400">{t.columns?.length ?? 0}</td>
                  <td className="px-4 py-3 text-gray-400 text-xs">
                    {t.created_at ? new Date(t.created_at).toLocaleDateString("zh-CN") : "—"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Link
                      to={`/data/${t.table_name}`}
                      className="text-xs text-blue-500 hover:text-blue-700 mr-3"
                    >
                      查看数据
                    </Link>
                    <form method="post" className="inline">
                      <input type="hidden" name="intent" value="delete" />
                      <input type="hidden" name="id" value={t.id} />
                      <button
                        type="submit"
                        onClick={(e) => { if (!confirm("确认删除注册信息？（不会删除实际数据表）")) e.preventDefault(); }}
                        className="text-xs text-red-400 hover:text-red-600"
                      >
                        取消注册
                      </button>
                    </form>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
