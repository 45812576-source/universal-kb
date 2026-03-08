import { useState } from "react";
import { useLoaderData, useFetcher } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

interface Tool {
  id: number;
  name: string;
  display_name: string;
  description: string;
  tool_type: "mcp" | "builtin" | "http";
  config: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_format: string;
  is_active: boolean;
  created_at: string;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const tools = await apiFetch("/api/tools", { token });
  return { tools, token };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;
  if (intent === "toggle") {
    const toolId = form.get("toolId") as string;
    const isActive = form.get("is_active") === "true";
    await apiFetch(`/api/tools/${toolId}`, { method: "PUT", body: JSON.stringify({ is_active: !isActive }), token });
  } else if (intent === "delete") {
    await apiFetch(`/api/tools/${form.get("toolId")}`, { method: "DELETE", token });
  } else if (intent === "create") {
    const body = {
      name: form.get("name") as string,
      display_name: form.get("display_name") as string,
      description: form.get("description") as string,
      tool_type: form.get("tool_type") as string,
      config: JSON.parse((form.get("config") as string) || "{}"),
      input_schema: JSON.parse((form.get("input_schema") as string) || "{}"),
      output_format: form.get("output_format") as string,
    };
    await apiFetch("/api/tools", { method: "POST", body: JSON.stringify(body), token });
  }
  return null;
}

const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  builtin: { label: "内置",  color: "bg-green-100 text-green-700 border-green-400" },
  mcp:     { label: "MCP",   color: "bg-purple-100 text-purple-700 border-purple-400" },
  http:    { label: "HTTP",  color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]" },
};

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">{children}</label>;
}

function PixelInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${props.className || ""}`} />;
}

function PixelSelect({ children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select {...props} className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]">
      {children}
    </select>
  );
}

export default function ToolsIndex() {
  const { tools, token } = useLoaderData<typeof loader>() as { tools: Tool[]; token: string };
  const fetcher = useFetcher();
  const [showCreate, setShowCreate] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testParams, setTestParams] = useState("{}");
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testLoading, setTestLoading] = useState(false);

  async function runTest(toolId: number) {
    setTestLoading(true);
    setTestResult(null);
    try {
      const resp = await fetch(`/api/tools/${toolId}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ params: JSON.parse(testParams) }),
      });
      const data = await resp.json();
      setTestResult(JSON.stringify(data, null, 2));
    } catch (e: any) {
      setTestResult(`Error: ${e.message}`);
    } finally {
      setTestLoading(false);
    }
  }

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">工具管理</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">注册和管理 MCP 工具、内置工具、HTTP 工具</p>
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
        >
          + 注册工具
        </button>
      </div>

      <div className="p-6 max-w-6xl">
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Tool_Registry</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <table className="w-full text-left">
            <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
              <tr>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">工具名称</th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">类型</th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">输出格式</th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">状态</th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {(tools || []).map((tool: Tool) => {
                const typeInfo = TYPE_LABELS[tool.tool_type] || { label: tool.tool_type, color: "bg-gray-100 text-gray-600 border-gray-400" };
                return (
                  <tr key={tool.id} className="hover:bg-[#F0F4F8]">
                    <td className="py-3 px-4">
                      <div className="text-xs font-bold text-[#1A202C]">{tool.display_name}</div>
                      <div className="text-[9px] font-bold uppercase text-gray-400">{tool.name}</div>
                      {tool.description && (
                        <div className="text-[9px] text-gray-500 mt-0.5 truncate max-w-48">{tool.description}</div>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${typeInfo.color}`}>
                        {typeInfo.label}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold uppercase text-gray-500">{tool.output_format}</td>
                    <td className="py-3 px-4">
                      <span className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${
                        tool.is_active
                          ? "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]"
                          : "bg-gray-100 text-gray-500 border-gray-400"
                      }`}>
                        {tool.is_active ? "启用" : "停用"}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center justify-end gap-3">
                        <button
                          onClick={() => { setTestingId(tool.id); setTestResult(null); setTestParams("{}"); }}
                          className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline"
                        >
                          测试
                        </button>
                        <fetcher.Form method="post" className="inline">
                          <input type="hidden" name="intent" value="toggle" />
                          <input type="hidden" name="toolId" value={tool.id} />
                          <input type="hidden" name="is_active" value={tool.is_active.toString()} />
                          <button type="submit" className="text-[10px] font-bold uppercase text-gray-500 hover:text-[#1A202C]">
                            {tool.is_active ? "停用" : "启用"}
                          </button>
                        </fetcher.Form>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {(tools || []).length === 0 && (
                <tr>
                  <td colSpan={5} className="py-12 text-center text-xs font-bold uppercase text-gray-400">暂无工具 — 点击右上角注册</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Test Modal */}
      {testingId && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="pixel-border bg-white w-full max-w-lg mx-4">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-widest">Tool_Test_Terminal</span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <div className="p-6 space-y-3">
              <div>
                <FieldLabel>参数 (JSON)</FieldLabel>
                <textarea
                  value={testParams}
                  onChange={(e) => setTestParams(e.target.value)}
                  rows={5}
                  className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-mono font-bold focus:outline-none focus:border-[#00D1FF]"
                />
              </div>
              {testResult && (
                <div className="border-2 border-[#1A202C] bg-[#F0F4F8] p-3">
                  <p className="text-[9px] font-bold uppercase text-gray-500 mb-1">执行结果</p>
                  <pre className="text-[10px] font-bold text-[#1A202C] whitespace-pre-wrap font-mono">{testResult}</pre>
                </div>
              )}
              <div className="flex gap-3">
                <button
                  onClick={() => runTest(testingId)}
                  disabled={testLoading}
                  className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
                >
                  {testLoading ? "执行中..." : "> 执行"}
                </button>
                <button
                  onClick={() => { setTestingId(null); setTestResult(null); }}
                  className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
                >
                  关闭
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Create Modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="pixel-border bg-white w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between sticky top-0">
              <span className="text-[10px] font-bold uppercase tracking-widest">注册工具</span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <fetcher.Form method="post" className="p-6 space-y-3" onSubmit={() => setShowCreate(false)}>
              <input type="hidden" name="intent" value="create" />
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <FieldLabel>工具ID <span className="text-[#00D1FF]">*</span></FieldLabel>
                  <PixelInput name="name" required placeholder="ppt_generator" />
                </div>
                <div>
                  <FieldLabel>显示名称 <span className="text-[#00D1FF]">*</span></FieldLabel>
                  <PixelInput name="display_name" required placeholder="PPT生成器" />
                </div>
              </div>
              <div>
                <FieldLabel>描述</FieldLabel>
                <PixelInput name="description" placeholder="工具功能说明" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <FieldLabel>类型 <span className="text-[#00D1FF]">*</span></FieldLabel>
                  <PixelSelect name="tool_type" defaultValue="builtin">
                    <option value="builtin">内置 (builtin)</option>
                    <option value="mcp">MCP</option>
                    <option value="http">HTTP</option>
                  </PixelSelect>
                </div>
                <div>
                  <FieldLabel>输出格式</FieldLabel>
                  <PixelSelect name="output_format" defaultValue="json">
                    <option value="json">JSON</option>
                    <option value="file">文件</option>
                    <option value="text">文本</option>
                  </PixelSelect>
                </div>
              </div>
              <div>
                <FieldLabel>配置 (JSON)</FieldLabel>
                <textarea name="config" rows={3} defaultValue="{}" className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-mono font-bold focus:outline-none focus:border-[#00D1FF]" />
              </div>
              <div>
                <FieldLabel>参数Schema (JSON)</FieldLabel>
                <textarea name="input_schema" rows={3} defaultValue="{}" className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-mono font-bold focus:outline-none focus:border-[#00D1FF]" />
              </div>
              <div className="flex gap-3 pt-2">
                <button type="submit" className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border">注册</button>
                <button type="button" onClick={() => setShowCreate(false)} className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100">取消</button>
              </div>
            </fetcher.Form>
          </div>
        </div>
      )}
    </div>
  );
}
