import { useState } from "react";
import { Link, useLoaderData } from "react-router";
import type { Route } from "./+types/generate";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  return { token };
}

type GenerateMode = "from_desc" | "from_table";

export default function GenerateBusinessTable() {
  const { token } = useLoaderData<typeof loader>() as { token: string };

  const [mode, setMode] = useState<GenerateMode>("from_desc");
  const [description, setDescription] = useState("");
  const [existingTable, setExistingTable] = useState("");
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<any>(null);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState("");

  async function generate() {
    setLoading(true);
    setError("");
    setPreview(null);
    try {
      if (mode === "from_desc") {
        const result = await apiFetch("/api/business-tables/generate", {
          method: "POST",
          body: JSON.stringify({ description }),
          token,
        });
        setPreview(result);
      } else {
        const result = await apiFetch("/api/business-tables/generate-from-existing", {
          method: "POST",
          body: JSON.stringify({ table_name: existingTable }),
          token,
        });
        setPreview({ ...result, table_name: existingTable, display_name: existingTable });
      }
    } catch (e: any) {
      setError(e.message || "生成失败");
    } finally {
      setLoading(false);
    }
  }

  async function applyPreview() {
    if (!preview) return;
    setApplying(true);
    setError("");
    try {
      await apiFetch("/api/business-tables/apply", {
        method: "POST",
        body: JSON.stringify({
          table_name: preview.table_name,
          display_name: preview.display_name,
          description: preview.description || "",
          ddl_sql: mode === "from_desc" ? preview.ddl_sql : "",
          validation_rules: preview.validation_rules || {},
          workflow: preview.workflow || {},
          create_skill: true,
          skill_def: preview.skill,
        }),
        token,
      });
      setApplied(true);
    } catch (e: any) {
      setError(e.message || "应用失败");
    } finally {
      setApplying(false);
    }
  }

  if (applied) {
    return (
      <div className="p-6 max-w-3xl">
        <div className="rounded-xl bg-green-50 border border-green-200 p-8 text-center">
          <p className="text-green-700 font-semibold text-lg mb-2">创建成功！</p>
          <p className="text-green-600 text-sm mb-4">
            数据表「{preview?.display_name}」已注册，并自动创建了对应 Skill
          </p>
          <div className="flex gap-3 justify-center">
            <Link
              to="/admin/business-tables"
              className="rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700"
            >
              查看业务表列表
            </Link>
            <Link
              to={`/data/${preview?.table_name}`}
              className="rounded-lg border border-green-200 px-4 py-2 text-sm font-medium text-green-700 hover:bg-green-100"
            >
              查看数据
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-4xl">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <Link to="/admin/business-tables" className="text-gray-400 hover:text-gray-600 text-sm">
          ← 业务数据表
        </Link>
        <span className="text-gray-300">/</span>
        <h1 className="text-xl font-bold text-gray-900">生成业务数据表</h1>
      </div>

      {/* Mode switch */}
      <div className="flex gap-2 mb-6">
        <button
          onClick={() => { setMode("from_desc"); setPreview(null); }}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
            mode === "from_desc"
              ? "bg-blue-600 text-white"
              : "border border-gray-200 text-gray-600 hover:bg-gray-50"
          }`}
        >
          方向A：描述→生成表+Skill
        </button>
        <button
          onClick={() => { setMode("from_table"); setPreview(null); }}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
            mode === "from_table"
              ? "bg-blue-600 text-white"
              : "border border-gray-200 text-gray-600 hover:bg-gray-50"
          }`}
        >
          方向B：已有表→生成Skill
        </button>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 p-5 space-y-4">
        {mode === "from_desc" ? (
          <>
            <h2 className="text-sm font-semibold text-gray-700">描述业务场景</h2>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={6}
              placeholder="例：达人结算流程，需要记录达人姓名、联系方式、平台、合作项目、结算金额、佣金比例（不超过30%）、状态（submitted/approved/paid）、结算日期等信息..."
              className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400 resize-y"
            />
          </>
        ) : (
          <>
            <h2 className="text-sm font-semibold text-gray-700">输入已有表名</h2>
            <input
              value={existingTable}
              onChange={(e) => setExistingTable(e.target.value)}
              placeholder="例：knowledge_entries"
              className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
            />
          </>
        )}

        <button
          onClick={generate}
          disabled={loading || (mode === "from_desc" ? !description.trim() : !existingTable.trim())}
          className="rounded-lg bg-blue-600 px-5 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {loading ? "AI 生成中..." : "生成预览"}
        </button>
      </div>

      {error && (
        <div className="mt-4 rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Preview */}
      {preview && (
        <div className="mt-6 space-y-4">
          <h2 className="text-base font-semibold text-gray-900">生成预览</h2>

          {/* Table info */}
          <div className="bg-white rounded-xl border border-gray-200 p-5 space-y-3">
            <div className="flex gap-4">
              <div>
                <p className="text-xs text-gray-400">表名</p>
                <p className="text-sm font-mono font-medium text-gray-800">{preview.table_name}</p>
              </div>
              <div>
                <p className="text-xs text-gray-400">显示名</p>
                <p className="text-sm font-medium text-gray-800">{preview.display_name}</p>
              </div>
            </div>
            {preview.description && (
              <p className="text-sm text-gray-500">{preview.description}</p>
            )}
          </div>

          {/* DDL */}
          {preview.ddl_sql && (
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <h3 className="text-sm font-semibold text-gray-700 mb-2">DDL SQL</h3>
              <pre className="text-xs font-mono bg-gray-50 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap text-gray-700">
                {preview.ddl_sql}
              </pre>
            </div>
          )}

          {/* Validation rules */}
          {preview.validation_rules && Object.keys(preview.validation_rules).length > 0 && (
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <h3 className="text-sm font-semibold text-gray-700 mb-2">校验规则</h3>
              <pre className="text-xs font-mono bg-gray-50 rounded-lg p-3 overflow-x-auto text-gray-700">
                {JSON.stringify(preview.validation_rules, null, 2)}
              </pre>
            </div>
          )}

          {/* Skill preview */}
          {preview.skill && (
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <h3 className="text-sm font-semibold text-gray-700 mb-2">
                自动生成 Skill：{preview.skill.name}
              </h3>
              <p className="text-sm text-gray-500 mb-3">{preview.skill.description}</p>
              <div>
                <p className="text-xs text-gray-400 mb-1">System Prompt 预览</p>
                <pre className="text-xs font-mono bg-gray-50 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap text-gray-700 max-h-40">
                  {preview.skill.system_prompt}
                </pre>
              </div>
              {preview.skill.data_queries?.length > 0 && (
                <div className="mt-3">
                  <p className="text-xs text-gray-400 mb-1">数据查询能力</p>
                  <div className="space-y-1">
                    {preview.skill.data_queries.map((q: any, i: number) => (
                      <div key={i} className="flex items-center gap-2 text-xs text-gray-600">
                        <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                          q.query_type === "read" ? "bg-blue-50 text-blue-600" : "bg-orange-50 text-orange-600"
                        }`}>
                          {q.query_type}
                        </span>
                        <span>{q.query_name}</span>
                        {q.description && <span className="text-gray-400">— {q.description}</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="flex gap-3">
            <button
              onClick={applyPreview}
              disabled={applying}
              className="rounded-lg bg-green-600 px-5 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:opacity-50 transition-colors"
            >
              {applying ? "创建中..." : "确认创建"}
            </button>
            <button
              onClick={() => setPreview(null)}
              className="rounded-lg border border-gray-200 px-5 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50 transition-colors"
            >
              重新生成
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
