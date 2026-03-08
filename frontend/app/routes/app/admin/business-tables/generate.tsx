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

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">{children}</label>;
}

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
      <div className="min-h-full bg-[#F0F4F8]">
        <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">生成业务数据表</h1>
        </div>
        <div className="p-6 max-w-3xl">
          <div className="pixel-border bg-white p-8 text-center">
            <div className="w-8 h-1 bg-[#00D1FF] mx-auto mb-4" />
            <p className="text-xs font-bold uppercase tracking-widest text-[#1A202C] mb-2">创建成功</p>
            <p className="text-[10px] text-gray-500 font-bold uppercase mb-6">
              数据表「{preview?.display_name}」已注册，并自动创建了对应 Skill
            </p>
            <div className="flex gap-3 justify-center">
              <Link
                to="/admin/business-tables"
                className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border"
              >
                查看业务表列表
              </Link>
              <Link
                to={`/data/${preview?.table_name}`}
                className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
              >
                查看数据
              </Link>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div className="flex items-center gap-2">
          <Link to="/admin/business-tables" className="text-[10px] font-bold uppercase tracking-widest text-gray-400 hover:text-[#1A202C]">
            业务数据表
          </Link>
          <span className="text-gray-300 font-bold">/</span>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">生成新数据表</h1>
        </div>
      </div>

      <div className="p-6 max-w-4xl space-y-4">
        {/* Mode switch */}
        <div className="flex gap-0 border-2 border-[#1A202C] w-fit">
          <button
            onClick={() => { setMode("from_desc"); setPreview(null); }}
            className={`px-4 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors ${
              mode === "from_desc" ? "bg-[#1A202C] text-white" : "bg-white text-gray-500 hover:bg-[#EBF4F7]"
            }`}
          >
            方向A：描述→生成表+Skill
          </button>
          <button
            onClick={() => { setMode("from_table"); setPreview(null); }}
            className={`px-4 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors border-l-2 border-[#1A202C] ${
              mode === "from_table" ? "bg-[#1A202C] text-white" : "bg-white text-gray-500 hover:bg-[#EBF4F7]"
            }`}
          >
            方向B：已有表→生成Skill
          </button>
        </div>

        {/* Input panel */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              {mode === "from_desc" ? "描述业务场景" : "输入已有表名"}
            </span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <div className="p-5 space-y-4">
            {mode === "from_desc" ? (
              <div>
                <FieldLabel>业务场景描述</FieldLabel>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={6}
                  placeholder="例：达人结算流程，需要记录达人姓名、联系方式、平台、合作项目、结算金额、佣金比例（不超过30%）、状态（submitted/approved/paid）、结算日期等信息..."
                  className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-y"
                />
              </div>
            ) : (
              <div>
                <FieldLabel>已有表名</FieldLabel>
                <input
                  value={existingTable}
                  onChange={(e) => setExistingTable(e.target.value)}
                  placeholder="例：knowledge_entries"
                  className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] font-mono"
                />
              </div>
            )}
            <button
              onClick={generate}
              disabled={loading || (mode === "from_desc" ? !description.trim() : !existingTable.trim())}
              className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
            >
              {loading ? "AI 生成中..." : "> 生成预览"}
            </button>
          </div>
        </div>

        {error && (
          <div className="border-2 border-red-400 bg-red-50 px-4 py-3 text-xs font-bold text-red-700">
            {error}
          </div>
        )}

        {/* Preview */}
        {preview && (
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <div className="w-1 h-4 bg-[#00D1FF]" />
              <span className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">生成预览</span>
            </div>

            {/* Table info */}
            <div className="pixel-border bg-white overflow-hidden">
              <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
                <span className="text-[10px] font-bold uppercase tracking-widest">Table_Info</span>
                <div className="flex space-x-1.5">
                  <div className="w-2 h-2 bg-red-400" />
                  <div className="w-2 h-2 bg-yellow-400" />
                  <div className="w-2 h-2 bg-green-400" />
                </div>
              </div>
              <div className="p-4 space-y-2">
                <div className="flex gap-6">
                  <div>
                    <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500">表名</p>
                    <p className="text-xs font-mono font-bold text-[#1A202C] mt-0.5">{preview.table_name}</p>
                  </div>
                  <div>
                    <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500">显示名</p>
                    <p className="text-xs font-bold text-[#1A202C] mt-0.5">{preview.display_name}</p>
                  </div>
                </div>
                {preview.description && (
                  <p className="text-[10px] text-gray-500 font-bold">{preview.description}</p>
                )}
              </div>
            </div>

            {/* DDL */}
            {preview.ddl_sql && (
              <div className="pixel-border bg-white overflow-hidden">
                <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C]">
                  <span className="text-[10px] font-bold uppercase tracking-widest">DDL_SQL</span>
                </div>
                <div className="p-4">
                  <pre className="text-[10px] font-mono font-bold bg-[#F0F4F8] border-2 border-[#1A202C] p-3 overflow-x-auto whitespace-pre-wrap text-[#1A202C]">
                    {preview.ddl_sql}
                  </pre>
                </div>
              </div>
            )}

            {/* Validation rules */}
            {preview.validation_rules && Object.keys(preview.validation_rules).length > 0 && (
              <div className="pixel-border bg-white overflow-hidden">
                <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C]">
                  <span className="text-[10px] font-bold uppercase tracking-widest">Validation_Rules</span>
                </div>
                <div className="p-4">
                  <pre className="text-[10px] font-mono font-bold bg-[#F0F4F8] border-2 border-[#1A202C] p-3 overflow-x-auto text-[#1A202C]">
                    {JSON.stringify(preview.validation_rules, null, 2)}
                  </pre>
                </div>
              </div>
            )}

            {/* Skill preview */}
            {preview.skill && (
              <div className="pixel-border bg-white overflow-hidden">
                <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C]">
                  <span className="text-[10px] font-bold uppercase tracking-widest">Skill_Preview: {preview.skill.name}</span>
                </div>
                <div className="p-4 space-y-3">
                  <p className="text-[10px] font-bold text-gray-500">{preview.skill.description}</p>
                  <div>
                    <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">System Prompt 预览</p>
                    <pre className="text-[10px] font-mono font-bold bg-[#F0F4F8] border-2 border-[#1A202C] p-3 overflow-x-auto whitespace-pre-wrap text-[#1A202C] max-h-40">
                      {preview.skill.system_prompt}
                    </pre>
                  </div>
                  {preview.skill.data_queries?.length > 0 && (
                    <div>
                      <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">数据查询能力</p>
                      <div className="space-y-1">
                        {preview.skill.data_queries.map((q: any, i: number) => (
                          <div key={i} className="flex items-center gap-2 text-[10px] font-bold text-gray-600">
                            <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${
                              q.query_type === "read"
                                ? "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]"
                                : "bg-orange-50 text-orange-700 border-orange-300"
                            }`}>
                              {q.query_type}
                            </span>
                            <span>{q.query_name}</span>
                            {q.description && <span className="text-gray-400 font-normal">— {q.description}</span>}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

            <div className="flex gap-3">
              <button
                onClick={applyPreview}
                disabled={applying}
                className="bg-[#1A202C] text-[#00D1FF] px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
              >
                {applying ? "创建中..." : "> 确认创建"}
              </button>
              <button
                onClick={() => setPreview(null)}
                className="border-2 border-[#1A202C] bg-white px-5 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
              >
                重新生成
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
