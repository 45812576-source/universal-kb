import { useState } from "react";

export interface ColumnDef {
  name: string;
  type: string;
  nullable: boolean;
  comment?: string;
}

export interface DataTableProps {
  columns: string[];
  columnDefs?: ColumnDef[];
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onRowSave?: (rowId: number, data: Record<string, unknown>) => Promise<void>;
  onRowCreate?: (data: Record<string, unknown>) => Promise<void>;
  onRowDelete?: (rowId: number) => Promise<void>;
  validationRules?: Record<string, { max?: number; min?: number; enum?: string[] }>;
}

function getInputType(colType: string): string {
  if (["int", "integer", "bigint", "tinyint", "smallint"].includes(colType)) return "number";
  if (["float", "double", "decimal"].includes(colType)) return "number";
  if (["date"].includes(colType)) return "date";
  if (["datetime", "timestamp"].includes(colType)) return "datetime-local";
  return "text";
}

export default function DataTable({
  columns,
  columnDefs = [],
  rows,
  total,
  page,
  pageSize,
  onPageChange,
  onRowSave,
  onRowCreate,
  onRowDelete,
  validationRules = {},
}: DataTableProps) {
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editData, setEditData] = useState<Record<string, unknown>>({});
  const [showNewRow, setShowNewRow] = useState(false);
  const [newRowData, setNewRowData] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const colDefMap = Object.fromEntries(columnDefs.map((c) => [c.name, c]));
  const editableColumns = columns.filter((c) => c !== "id" && c !== "created_at" && c !== "updated_at");
  const totalPages = Math.ceil(total / pageSize);

  function startEdit(row: Record<string, unknown>) {
    setEditingId(row.id as number);
    setEditData({ ...row });
    setError("");
  }

  function cancelEdit() {
    setEditingId(null);
    setEditData({});
  }

  async function saveEdit(rowId: number) {
    if (!onRowSave) return;
    setSaving(true);
    setError("");
    try {
      await onRowSave(rowId, editData);
      setEditingId(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function saveNewRow() {
    if (!onRowCreate) return;
    setSaving(true);
    setError("");
    try {
      await onRowCreate(newRowData);
      setShowNewRow(false);
      setNewRowData({});
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function deleteRow(rowId: number) {
    if (!onRowDelete) return;
    if (!confirm("确认删除这行数据？")) return;
    setSaving(true);
    try {
      await onRowDelete(rowId);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  function renderCell(col: string, value: unknown, isEditing: boolean, data: Record<string, unknown>, onChange: (col: string, val: unknown) => void) {
    if (!isEditing) {
      return <span className="text-sm text-gray-800">{String(value ?? "")}</span>;
    }

    const colDef = colDefMap[col];
    const rules = validationRules[col];
    const inputType = colDef ? getInputType(colDef.type) : "text";

    // Enum → select
    if (rules?.enum) {
      return (
        <select
          value={String(data[col] ?? "")}
          onChange={(e) => onChange(col, e.target.value)}
          className="w-full rounded border border-blue-300 px-2 py-1 text-sm"
        >
          <option value="">-- 选择 --</option>
          {rules.enum.map((opt) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
      );
    }

    return (
      <input
        type={inputType}
        value={String(data[col] ?? "")}
        onChange={(e) => onChange(col, e.target.value)}
        className="w-full rounded border border-blue-300 px-2 py-1 text-sm"
      />
    );
  }

  return (
    <div className="space-y-3">
      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* New row button */}
      {onRowCreate && !showNewRow && (
        <button
          onClick={() => setShowNewRow(true)}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 transition-colors"
        >
          + 新增行
        </button>
      )}

      <div className="overflow-x-auto rounded-xl border border-gray-200">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              {columns.map((col) => (
                <th key={col} className="px-4 py-2.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">
                  {colDefMap[col]?.comment || col}
                </th>
              ))}
              {(onRowSave || onRowDelete) && (
                <th className="px-4 py-2.5 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">
                  操作
                </th>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {/* New row form */}
            {showNewRow && (
              <tr className="bg-blue-50">
                {columns.map((col) => (
                  <td key={col} className="px-4 py-2">
                    {col === "id" || col === "created_at" || col === "updated_at" ? (
                      <span className="text-xs text-gray-400">自动</span>
                    ) : (
                      renderCell(col, newRowData[col], true, newRowData, (c, v) =>
                        setNewRowData((prev) => ({ ...prev, [c]: v }))
                      )
                    )}
                  </td>
                ))}
                {(onRowSave || onRowDelete) && (
                  <td className="px-4 py-2 text-right whitespace-nowrap">
                    <button
                      onClick={saveNewRow}
                      disabled={saving}
                      className="text-xs text-blue-600 hover:text-blue-800 mr-2 disabled:opacity-50"
                    >
                      保存
                    </button>
                    <button
                      onClick={() => { setShowNewRow(false); setNewRowData({}); }}
                      className="text-xs text-gray-400 hover:text-gray-600"
                    >
                      取消
                    </button>
                  </td>
                )}
              </tr>
            )}

            {rows.map((row) => {
              const rowId = row.id as number;
              const isEditing = editingId === rowId;
              return (
                <tr key={rowId} className={isEditing ? "bg-blue-50" : "hover:bg-gray-50"}>
                  {columns.map((col) => (
                    <td key={col} className="px-4 py-2.5">
                      {isEditing && editableColumns.includes(col)
                        ? renderCell(col, row[col], true, editData, (c, v) =>
                            setEditData((prev) => ({ ...prev, [c]: v }))
                          )
                        : renderCell(col, row[col], false, row, () => {})}
                    </td>
                  ))}
                  {(onRowSave || onRowDelete) && (
                    <td className="px-4 py-2.5 text-right whitespace-nowrap">
                      {isEditing ? (
                        <>
                          <button
                            onClick={() => saveEdit(rowId)}
                            disabled={saving}
                            className="text-xs text-blue-600 hover:text-blue-800 mr-2 disabled:opacity-50"
                          >
                            保存
                          </button>
                          <button
                            onClick={cancelEdit}
                            className="text-xs text-gray-400 hover:text-gray-600"
                          >
                            取消
                          </button>
                        </>
                      ) : (
                        <>
                          {onRowSave && (
                            <button
                              onClick={() => startEdit(row)}
                              className="text-xs text-gray-500 hover:text-blue-600 mr-2"
                            >
                              编辑
                            </button>
                          )}
                          {onRowDelete && (
                            <button
                              onClick={() => deleteRow(rowId)}
                              className="text-xs text-gray-400 hover:text-red-500"
                            >
                              删除
                            </button>
                          )}
                        </>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}

            {rows.length === 0 && (
              <tr>
                <td colSpan={columns.length + 1} className="px-4 py-8 text-center text-sm text-gray-400">
                  暂无数据
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm text-gray-500">
          <span>共 {total} 条</span>
          <div className="flex gap-1">
            <button
              onClick={() => onPageChange(page - 1)}
              disabled={page <= 1}
              className="rounded px-3 py-1 border border-gray-200 hover:bg-gray-50 disabled:opacity-40"
            >
              上一页
            </button>
            <span className="px-3 py-1">第 {page} / {totalPages} 页</span>
            <button
              onClick={() => onPageChange(page + 1)}
              disabled={page >= totalPages}
              className="rounded px-3 py-1 border border-gray-200 hover:bg-gray-50 disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
