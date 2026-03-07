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
    await apiFetch(`/api/tools/${toolId}`, {
      method: "PUT",
      body: JSON.stringify({ is_active: !isActive }),
      token,
    });
  } else if (intent === "delete") {
    const toolId = form.get("toolId") as string;
    await apiFetch(`/api/tools/${toolId}`, { method: "DELETE", token });
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
  builtin: { label: "内置", color: "bg-green-100 text-green-700" },
  mcp: { label: "MCP", color: "bg-purple-100 text-purple-700" },
  http: { label: "HTTP", color: "bg-blue-100 text-blue-700" },
};

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
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
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
    <div className="p-6 max-w-6xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-gray-900">工具管理</h1>
          <p className="text-sm text-gray-500 mt-0.5">注册和管理MCP工具、内置工具、HTTP工具</p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          + 注册工具
        </button>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-100 bg-gray-50">
              <th className="text-left py-3 px-4 font-medium text-gray-600">工具名称</th>
              <th className="text-left py-3 px-4 font-medium text-gray-600">类型</th>
              <th className="text-left py-3 px-4 font-medium text-gray-600">输出格式</th>
              <th className="text-left py-3 px-4 font-medium text-gray-600">状态</th>
              <th className="text-right py-3 px-4 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {(tools || []).map((tool: Tool) => {
              const typeInfo = TYPE_LABELS[tool.tool_type] || { label: tool.tool_type, color: "bg-gray-100" };
              return (
                <tr key={tool.id} className="hover:bg-gray-50">
                  <td className="py-3 px-4">
                    <div className="font-medium text-gray-900">{tool.display_name}</div>
                    <div className="text-xs text-gray-400 font-mono">{tool.name}</div>
                    {tool.description && (
                      <div className="text-xs text-gray-500 mt-0.5 truncate max-w-48">{tool.description}</div>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${typeInfo.color}`}>
                      {typeInfo.label}
                    </span>
                  </td>
                  <td className="py-3 px-4 text-gray-500 text-xs">{tool.output_format}</td>
                  <td className="py-3 px-4">
                    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${tool.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"}`}>
                      {tool.is_active ? "启用" : "停用"}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center justify-end gap-3">
                      <button
                        onClick={() => {
                          setTestingId(tool.id);
                          setTestResult(null);
                          setTestParams("{}");
                        }}
                        className="text-blue-600 hover:text-blue-700 text-xs font-medium"
                      >
                        测试
                      </button>
                      <fetcher.Form method="post" className="inline">
                        <input type="hidden" name="intent" value="toggle" />
                        <input type="hidden" name="toolId" value={tool.id} />
                        <input type="hidden" name="is_active" value={tool.is_active.toString()} />
                        <button type="submit" className="text-gray-500 hover:text-gray-700 text-xs">
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
                <td colSpan={5} className="py-12 text-center text-gray-400">暂无工具，点击右上角注册</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Test Modal */}
      {testingId && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 p-6">
            <h3 className="text-base font-semibold text-gray-900 mb-4">测试工具</h3>
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">参数 (JSON)</label>
                <textarea
                  value={testParams}
                  onChange={(e) => setTestParams(e.target.value)}
                  rows={5}
                  className="w-full rounded-lg border border-gray-200 px-3 py-2 text-xs font-mono focus:border-blue-400 focus:outline-none"
                />
              </div>
              {testResult && (
                <div className="rounded-lg bg-gray-50 border border-gray-200 p-3">
                  <p className="text-xs font-medium text-gray-600 mb-1">执行结果</p>
                  <pre className="text-xs text-gray-800 whitespace-pre-wrap">{testResult}</pre>
                </div>
              )}
              <div className="flex gap-3">
                <button
                  onClick={() => runTest(testingId)}
                  disabled={testLoading}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  {testLoading ? "执行中..." : "执行"}
                </button>
                <button
                  onClick={() => { setTestingId(null); setTestResult(null); }}
                  className="rounded-lg border border-gray-200 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50"
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
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-base font-semibold text-gray-900 mb-4">注册工具</h3>
            <fetcher.Form method="post" className="space-y-3" onSubmit={() => setShowCreate(false)}>
              <input type="hidden" name="intent" value="create" />
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">工具ID *</label>
                  <input name="name" required placeholder="ppt_generator" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">显示名称 *</label>
                  <input name="display_name" required placeholder="PPT生成器" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm" />
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
                <input name="description" placeholder="工具功能说明" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">类型 *</label>
                  <select name="tool_type" defaultValue="builtin" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm">
                    <option value="builtin">内置 (builtin)</option>
                    <option value="mcp">MCP</option>
                    <option value="http">HTTP</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">输出格式</label>
                  <select name="output_format" defaultValue="json" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm">
                    <option value="json">JSON</option>
                    <option value="file">文件</option>
                    <option value="text">文本</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">配置 (JSON)</label>
                <textarea name="config" rows={3} defaultValue="{}" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-xs font-mono" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">参数Schema (JSON)</label>
                <textarea name="input_schema" rows={3} defaultValue="{}" className="w-full rounded-lg border border-gray-200 px-3 py-2 text-xs font-mono" />
              </div>
              <div className="flex gap-3 pt-2">
                <button type="submit" className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">注册</button>
                <button type="button" onClick={() => setShowCreate(false)} className="rounded-lg border border-gray-200 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50">取消</button>
              </div>
            </fetcher.Form>
          </div>
        </div>
      )}
    </div>
  );
}
