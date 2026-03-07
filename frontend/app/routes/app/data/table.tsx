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
    // Force reload with new page via URL
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
    <div className="p-6 max-w-full">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <Link to="/data" className="text-gray-400 hover:text-gray-600 text-sm">
          ← 业务数据
        </Link>
        <span className="text-gray-300">/</span>
        <div>
          <h1 className="text-xl font-bold text-gray-900">{schema.display_name}</h1>
          <p className="text-xs text-gray-400 font-mono">{tableName}</p>
        </div>
      </div>

      {schema.description && (
        <p className="text-sm text-gray-500 mb-4">{schema.description}</p>
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
  );
}
