import { useState, useEffect } from "react";
import { useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

interface McpToken {
  id: number;
  prefix: string;
  scope: "user" | "workspace" | "admin";
  workspace_id: number | null;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const tokens = await apiFetch("/api/mcp-tokens", { token });
  return { tokens, token, user };
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="block text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
      {children}
    </label>
  );
}

function PixelInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${props.className || ""}`}
    />
  );
}

function PixelSelect({
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
    >
      {children}
    </select>
  );
}

const SCOPE_BADGES: Record<string, { label: string; color: string }> = {
  user: {
    label: "普通用户",
    color: "bg-gray-100 text-gray-600 border-gray-400",
  },
  workspace: {
    label: "工作台",
    color: "bg-[#BEE3F8] text-[#2B6CB0] border-[#3182CE]",
  },
  admin: {
    label: "全局管理员",
    color: "bg-[#CCF2FF] text-[#00A3C4] border-[#00D1FF]",
  },
};

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function McpTokensIndex() {
  const initial = useLoaderData<typeof loader>() as {
    tokens: McpToken[];
    token: string;
    user: { role: string };
  };

  const [tokens, setTokens] = useState<McpToken[]>(initial.tokens || []);
  const [authToken] = useState(initial.token);

  // Create form state
  const [scope, setScope] = useState<"user" | "workspace" | "admin">("user");
  const [workspaceId, setWorkspaceId] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // New token reveal state
  const [newRawToken, setNewRawToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // MCP URL
  const [mcpOrigin, setMcpOrigin] = useState("");
  useEffect(() => {
    if (typeof window !== "undefined") {
      setMcpOrigin(window.location.origin);
    }
  }, []);

  async function refreshTokens() {
    try {
      const data = await apiFetch("/api/mcp-tokens", { token: authToken });
      setTokens(data || []);
    } catch {
      // ignore
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      const body: Record<string, unknown> = { scope };
      if (scope === "workspace" && workspaceId) {
        body.workspace_id = parseInt(workspaceId, 10);
      }
      if (expiresDays) {
        body.expires_days = parseInt(expiresDays, 10);
      }
      const result = await apiFetch("/api/mcp-tokens", {
        method: "POST",
        body: JSON.stringify(body),
        token: authToken,
      });
      // result should contain the raw token
      setNewRawToken(result.token || result.raw_token || JSON.stringify(result));
      setCopied(false);
      // Reset form
      setScope("user");
      setWorkspaceId("");
      setExpiresDays("");
    } catch (err: unknown) {
      setCreateError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(id: number) {
    if (!window.confirm("确定要删除此 Token？删除后无法恢复。")) return;
    try {
      await apiFetch(`/api/mcp-tokens/${id}`, {
        method: "DELETE",
        token: authToken,
      });
      await refreshTokens();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "删除失败");
    }
  }

  function handleCopied() {
    if (newRawToken) {
      navigator.clipboard.writeText(newRawToken).catch(() => {});
      setCopied(true);
    }
  }

  function handleDismissToken() {
    setNewRawToken(null);
    setCopied(false);
    refreshTokens();
  }

  const mcpServerUrl = mcpOrigin ? `${mcpOrigin}/mcp` : "/mcp";
  const exampleConfig = `{
  "mcpServers": {
    "universal-kb": {
      "url": "${mcpServerUrl}",
      "headers": {
        "Authorization": "Bearer <YOUR_TOKEN>"
      }
    }
  }
}`;

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      {/* Page header */}
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">
              MCP Token 管理
            </h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">
              生成和管理 MCP 访问令牌
            </p>
          </div>
        </div>
      </div>

      <div className="p-6 max-w-4xl space-y-6">
        {/* New token reveal */}
        {newRawToken && (
          <div className="pixel-border bg-white border-2 border-[#00D1FF]">
            <div className="bg-[#CCF2FF] px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-widest text-[#00A3C4]">
                Token 已生成
              </span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <div className="p-4 space-y-3">
              <p className="text-[10px] font-bold text-red-600 uppercase tracking-wide">
                ⚠ 请立即复制，关闭后无法再次查看
              </p>
              <div className="border-2 border-[#1A202C] bg-[#F0F4F8] px-4 py-3 font-mono text-xs font-bold text-[#1A202C] break-all select-all">
                {newRawToken}
              </div>
              <div className="flex gap-3">
                <button
                  onClick={handleCopied}
                  className="bg-[#00D1FF] text-[#1A202C] px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-[#00B8E0] pixel-border"
                >
                  {copied ? "✓ 已复制" : "复制 Token"}
                </button>
                <button
                  onClick={handleDismissToken}
                  disabled={!copied}
                  className="border-2 border-[#1A202C] bg-white px-4 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  我已复制，关闭
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Create token form */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              生成新 Token
            </span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <form onSubmit={handleCreate} className="p-4 space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <div>
                <FieldLabel>
                  权限范围 <span className="text-[#00D1FF]">*</span>
                </FieldLabel>
                <PixelSelect
                  value={scope}
                  onChange={(e) =>
                    setScope(e.target.value as "user" | "workspace" | "admin")
                  }
                >
                  <option value="user">普通用户 (user)</option>
                  <option value="workspace">工作台 (workspace)</option>
                  <option value="admin">全局管理员 (admin)</option>
                </PixelSelect>
              </div>
              {scope === "workspace" && (
                <div>
                  <FieldLabel>工作台 ID</FieldLabel>
                  <PixelInput
                    type="number"
                    value={workspaceId}
                    onChange={(e) => setWorkspaceId(e.target.value)}
                    placeholder="输入工作台 ID"
                    min={1}
                  />
                </div>
              )}
              <div>
                <FieldLabel>有效天数</FieldLabel>
                <PixelInput
                  type="number"
                  value={expiresDays}
                  onChange={(e) => setExpiresDays(e.target.value)}
                  placeholder="不填则永不过期"
                  min={1}
                />
              </div>
            </div>
            {createError && (
              <p className="text-[10px] font-bold text-red-600 uppercase">
                错误: {createError}
              </p>
            )}
            <div>
              <button
                type="submit"
                disabled={creating}
                className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
              >
                {creating ? "生成中..." : "+ 生成 Token"}
              </button>
            </div>
          </form>
        </div>

        {/* Token list */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              Token_Registry
            </span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <table className="w-full text-left">
            <thead className="bg-[#F0F4F8] border-b-2 border-[#1A202C]">
              <tr>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">
                  Token 前缀
                </th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">
                  权限范围
                </th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">
                  工作台 ID
                </th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">
                  过期时间
                </th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500">
                  最近使用
                </th>
                <th className="py-3 px-4 text-[9px] font-bold uppercase tracking-widest text-gray-500 text-right">
                  操作
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {tokens.map((t) => {
                const badge =
                  SCOPE_BADGES[t.scope] || SCOPE_BADGES.user;
                return (
                  <tr key={t.id} className="hover:bg-[#F0F4F8]">
                    <td className="py-3 px-4">
                      <span className="font-mono text-xs font-bold text-[#1A202C]">
                        {t.prefix}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span
                        className={`inline-block border px-1.5 py-0.5 text-[9px] font-bold uppercase ${badge.color}`}
                      >
                        {badge.label}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold text-gray-500">
                      {t.workspace_id ?? "—"}
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold text-gray-500">
                      {t.expires_at ? formatDate(t.expires_at) : "永不过期"}
                    </td>
                    <td className="py-3 px-4 text-[10px] font-bold text-gray-500">
                      {t.last_used_at ? formatDate(t.last_used_at) : "从未使用"}
                    </td>
                    <td className="py-3 px-4 text-right">
                      <button
                        onClick={() => handleDelete(t.id)}
                        className="text-[10px] font-bold uppercase text-red-500 hover:text-red-700 hover:underline"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                );
              })}
              {tokens.length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    className="py-12 text-center text-xs font-bold uppercase text-gray-400"
                  >
                    暂无 Token — 在上方生成
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* MCP 使用说明 */}
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">
              MCP_Usage_Guide
            </span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <div className="p-4 space-y-4">
            <div>
              <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
                MCP Server URL
              </p>
              <div className="border-2 border-[#1A202C] bg-[#F0F4F8] px-4 py-2 font-mono text-xs font-bold text-[#1A202C] break-all">
                {mcpServerUrl}
              </div>
            </div>
            <div>
              <p className="text-[9px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
                Claude Code 配置示例 (~/.claude/settings.json 或项目 .mcp.json)
              </p>
              <pre className="border-2 border-[#1A202C] bg-[#F0F4F8] px-4 py-3 text-xs font-mono font-bold text-[#1A202C] whitespace-pre overflow-x-auto">
                {exampleConfig}
              </pre>
            </div>
            <p className="text-[9px] font-bold uppercase tracking-widest text-gray-400">
              将 &lt;YOUR_TOKEN&gt; 替换为上方生成的 Token 值。Token 只在生成时显示一次，请妥善保存。
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
