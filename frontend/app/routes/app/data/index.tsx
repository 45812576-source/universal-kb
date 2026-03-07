import { Link, useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

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
  return { tables };
}

export default function DataIndex() {
  const { tables } = useLoaderData<typeof loader>() as { tables: BusinessTable[] };

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">业务数据</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">查看和管理业务数据表</p>
          </div>
        </div>
        <Link
          to="/admin/business-tables"
          className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-gray-700 hover:bg-[#EBF4F7] transition-colors"
        >
          管理数据表
        </Link>
      </div>

      <div className="p-6 max-w-5xl">
        {tables.length === 0 ? (
          <div className="border-2 border-dashed border-[#1A202C] p-12 text-center bg-white">
            <p className="text-xs font-bold uppercase text-gray-400">暂无业务数据表</p>
            <p className="text-[10px] font-bold uppercase text-gray-300 mt-1">
              请先在「管理 → 业务数据表」中注册
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {tables.map((t) => (
              <Link
                key={t.id}
                to={`/data/${t.table_name}`}
                className="block pixel-border bg-white p-5 hover:bg-[#EBF4F7] transition-colors"
              >
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="text-xs font-bold uppercase text-[#1A202C]">{t.display_name}</h3>
                    <p className="text-[9px] font-bold text-gray-400 mt-0.5 uppercase">{t.table_name}</p>
                  </div>
                  <span className="text-[9px] font-bold uppercase bg-[#EBF4F7] border border-[#1A202C] px-2 py-0.5 text-gray-600">
                    {t.columns?.length ?? 0} 列
                  </span>
                </div>
                {t.description && (
                  <p className="text-xs text-gray-500 mt-2 line-clamp-2">{t.description}</p>
                )}
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
