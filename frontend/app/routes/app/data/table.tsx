import { useState } from "react";
import { Link, useLoaderData, useParams, useRevalidator } from "react-router";
import type { Route } from "./+types/table";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import DataTable from "~/components/DataTable";

interface TableSchema {
  table_name: string;
  display_name: string;
  description: string;
  columns: { name: string; type: string; nullable: boolean; comment: string }[];
  validation_rules: Record<string, { max?: number; min?: number; enum?: string[] }>;
}

interface RowsResponse {
  total: number;
  page: number;
  page_size: number;
  columns: string[];
  rows: Record<string, unknown>[];
}

export async function loader({ request, params }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const { tableName } = params;
  const url = new URL(request.url);
  const page = Number(url.searchParams.get("page") || "1");

  const [schema, rowsData] = await Promise.all([
    apiFetch(`/api/data/${tableName}/schema`, { token }),
    apiFetch(`/api/data/${tableName}/rows?page=${page}&page_size=20`, { token }),
  ]);

  return { schema, rowsData, token, page };
}

export default function TableView() {
  const { schema, rowsData, token, page: initialPage } = useLoaderData<typeof loader>() as {
    schema: TableSchema;
    rowsData: RowsResponse;
    token: string;
    page: number;
  };
  const { tableName } = useParams();
  const revalidator = useRevalidator();
  const [page, setPage] = useState(initialPage);

  async function handlePageChange(newPage: number) {
    setPage(newPage);
    window.history.pushState({}, "", `?page=${newPage}`);
    revalidator.revalidate();
  }

  async function handleRowSave(rowId: number, data: Record<string, unknown>) {
    await apiFetch(`/api/data/${tableName}/rows/${rowId}`, {
      method: "PUT",
      body: JSON.stringify({ data }),
      token,
    });
    revalidator.revalidate();
  }

  async function handleRowCreate(data: Record<string, unknown>) {
    await apiFetch(`/api/data/${tableName}/rows`, {
      method: "POST",
      body: JSON.stringify({ data }),
      token,
    });
    revalidator.revalidate();
  }

  async function handleRowDelete(rowId: number) {
    await apiFetch(`/api/data/${tableName}/rows/${rowId}`, {
      method: "DELETE",
      token,
    });
    revalidator.revalidate();
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div className="flex items-center gap-2">
          <Link to="/data" className="text-[10px] font-bold uppercase tracking-widest text-gray-400 hover:text-[#1A202C]">
            业务数据
          </Link>
          <span className="text-gray-300 font-bold">/</span>
          <div>
            <span className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">{schema.display_name}</span>
            <span className="ml-2 text-[9px] font-mono font-bold text-gray-400">{tableName}</span>
          </div>
        </div>
      </div>

      <div className="p-6">
        {schema.description && (
          <p className="text-[10px] font-bold text-gray-500 mb-4 uppercase">{schema.description}</p>
        )}

        <DataTable
          columns={rowsData.columns}
          columnDefs={schema.columns}
          rows={rowsData.rows}
          total={rowsData.total}
          page={page}
          pageSize={20}
          onPageChange={handlePageChange}
          onRowSave={handleRowSave}
          onRowCreate={handleRowCreate}
          onRowDelete={handleRowDelete}
          validationRules={schema.validation_rules}
        />
      </div>
    </div>
  );
}
