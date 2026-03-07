import { useState } from "react";
import { Link, useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

interface MarketSource {
  id: number;
  name: string;
  base_url: string;
  description: string;
  is_active: boolean;
}

interface MarketSkill {
  id: string;
  name: string;
  description: string;
  version: string;
  author: string;
  system_prompt: string;
  tags?: string[];
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  let sources: MarketSource[] = [];
  try {
    sources = await apiFetch("/api/skill-market/sources", { token });
  } catch {
    sources = [];
  }
  return { sources, token };
}

export default function SkillMarketIndex() {
  const { sources, token } = useLoaderData<typeof loader>() as {
    sources: MarketSource[];
    token: string;
  };

  const [selectedSource, setSelectedSource] = useState<MarketSource | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MarketSkill[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [previewSkill, setPreviewSkill] = useState<MarketSkill | null>(null);

  const [importing, setImporting] = useState<string | null>(null);
  const [importMsg, setImportMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);

  async function handleSearch() {
    if (!selectedSource) return;
    setSearching(true);
    setSearchError(null);
    setResults([]);
    setImportMsg(null);
    try {
      const params = new URLSearchParams({ source_id: String(selectedSource.id), q: query });
      const data = await apiFetch(`/api/skill-market/search?${params}`, { token });
      setResults(Array.isArray(data) ? data : data.results || []);
    } catch (e: unknown) {
      const err = e as Error;
      setSearchError(err.message || "搜索失败");
    } finally {
      setSearching(false);
    }
  }

  async function handleImport(skill: MarketSkill) {
    setImporting(skill.id);
    setImportMsg(null);
    try {
      await apiFetch("/api/skill-market/import", {
        method: "POST",
        body: JSON.stringify({ source_id: selectedSource?.id, skill_id: skill.id }),
        token,
      });
      setImportMsg({ type: "success", text: `导入成功：${skill.name}` });
    } catch (e: unknown) {
      const err = e as Error;
      setImportMsg({ type: "error", text: err.message || "导入失败" });
    } finally {
      setImporting(null);
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">Skill 外部市场</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">从外部市场搜索并导入 Skill</p>
          </div>
        </div>
      </div>

      <div className="p-6 max-w-5xl space-y-5">
        {/* Source selector */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Market_Sources</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <div className="p-4">
            {sources.length === 0 ? (
              <p className="text-xs font-bold uppercase text-gray-400">
                暂无可用市场源 —{" "}
                <Link
                  to="/admin/skill-market/sources"
                  className="text-[#00A3C4] hover:underline"
                >
                  前往配置市场源
                </Link>
              </p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {sources.map((src) => (
                  <button
                    key={src.id}
                    onClick={() => {
                      setSelectedSource(src);
                      setResults([]);
                      setImportMsg(null);
                      setSearchError(null);
                    }}
                    className={`px-3 py-1.5 text-[10px] font-bold uppercase tracking-wide border-2 transition-colors ${
                      selectedSource?.id === src.id
                        ? "bg-[#1A202C] text-white border-[#1A202C]"
                        : "bg-white text-[#1A202C] border-[#1A202C] hover:bg-[#EBF4F7]"
                    }`}
                  >
                    {src.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Search bar */}
        {selectedSource && (
          <div className="pixel-border bg-white overflow-hidden">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
              <span className="text-[10px] font-bold uppercase tracking-widest">
                Search — {selectedSource.name}
              </span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <div className="p-4 flex gap-3">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                placeholder="输入关键词搜索 Skill..."
                className="flex-1 border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
              />
              <button
                onClick={handleSearch}
                disabled={searching}
                className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
              >
                {searching ? "搜索中..." : "> 搜索"}
              </button>
            </div>
          </div>
        )}

        {/* Import message */}
        {importMsg && (
          <div
            className={`border-2 px-4 py-3 text-xs font-bold uppercase ${
              importMsg.type === "success"
                ? "border-[#00D1FF] bg-[#CCF2FF] text-[#00A3C4]"
                : "border-red-400 bg-red-50 text-red-700"
            }`}
          >
            {importMsg.type === "success" ? (
              <>
                {importMsg.text} —{" "}
                <Link to="/admin/skills" className="underline hover:no-underline">
                  前往 Skill 管理查看
                </Link>
              </>
            ) : (
              importMsg.text
            )}
          </div>
        )}

        {/* Search error */}
        {searchError && (
          <div className="border-2 border-red-400 bg-red-50 px-4 py-3 text-xs font-bold uppercase text-red-700">
            搜索失败：{searchError}
          </div>
        )}

        {/* Results */}
        {results.length > 0 && (
          <div className="pixel-border bg-white overflow-hidden">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
              <span className="text-[10px] font-bold uppercase tracking-widest">
                Search_Results ({results.length})
              </span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <div className="divide-y divide-gray-100">
              {results.map((skill) => (
                <div key={skill.id} className="px-4 py-4 hover:bg-[#F0F4F8] transition-colors">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-3 flex-wrap">
                        <span className="text-xs font-bold text-[#1A202C]">{skill.name}</span>
                        {skill.version && (
                          <span className="inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]">
                            v{skill.version}
                          </span>
                        )}
                        {skill.author && (
                          <span className="text-[9px] font-bold uppercase text-gray-400">
                            by {skill.author}
                          </span>
                        )}
                      </div>
                      {skill.description && (
                        <p className="text-[10px] text-gray-500 mt-1 leading-relaxed">
                          {skill.description}
                        </p>
                      )}
                      {skill.tags && skill.tags.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-2">
                          {skill.tags.map((tag) => (
                            <span
                              key={tag}
                              className="px-1.5 py-0.5 text-[9px] font-bold uppercase bg-gray-100 border border-gray-300 text-gray-500"
                            >
                              {tag}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-3 flex-shrink-0">
                      <button
                        onClick={() => setPreviewSkill(skill)}
                        className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                      >
                        预览
                      </button>
                      <button
                        onClick={() => handleImport(skill)}
                        disabled={importing === skill.id}
                        className="bg-[#1A202C] text-white px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
                      >
                        {importing === skill.id ? "导入中..." : "导入"}
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Empty state after search */}
        {!searching && selectedSource && results.length === 0 && !searchError && query && (
          <div className="pixel-border bg-white px-4 py-12 text-center">
            <p className="text-xs font-bold uppercase text-gray-400">未找到匹配的 Skill</p>
          </div>
        )}
      </div>

      {/* Preview Modal */}
      {previewSkill && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="pixel-border bg-white w-full max-w-2xl mx-4 max-h-[90vh] flex flex-col">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between flex-shrink-0">
              <span className="text-[10px] font-bold uppercase tracking-widest truncate mr-4">
                Preview — {previewSkill.name}
              </span>
              <div className="flex space-x-1.5 flex-shrink-0">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <div className="p-5 space-y-4 overflow-y-auto flex-1">
              <div>
                <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
                  System Prompt
                </p>
                <pre className="w-full border-2 border-[#1A202C] bg-[#F0F4F8] px-4 py-3 text-[10px] font-mono text-[#1A202C] whitespace-pre-wrap leading-relaxed overflow-y-auto max-h-80">
                  {previewSkill.system_prompt || "（无 system_prompt）"}
                </pre>
              </div>
              {previewSkill.description && (
                <div>
                  <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
                    描述
                  </p>
                  <p className="text-xs text-gray-600">{previewSkill.description}</p>
                </div>
              )}
            </div>
            <div className="px-5 pb-5 flex gap-3 flex-shrink-0">
              <button
                onClick={() => {
                  handleImport(previewSkill);
                  setPreviewSkill(null);
                }}
                disabled={importing === previewSkill.id}
                className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
              >
                {importing === previewSkill.id ? "导入中..." : "导入此 Skill"}
              </button>
              <button
                onClick={() => setPreviewSkill(null)}
                className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
