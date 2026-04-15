import { useState, useEffect } from "react";
import { Form, NavLink, Outlet, useLoaderData } from "react-router";
import type { Route } from "./+types/layout";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { User } from "~/lib/types";

export function shouldRevalidate({ formAction, currentUrl, nextUrl }: { formAction?: string; currentUrl: URL; nextUrl: URL }) {
  // 仅在路径变化时 revalidate，子路由 fetcher action 不触发
  if (formAction) return false;
  return currentUrl.pathname !== nextUrl.pathname;
}

export async function loader({ request }: Route.LoaderArgs) {
  const { user, token } = await requireUser(request);
  const taskStats = await apiFetch("/api/tasks/stats", { token }).catch(() => ({ total_pending: 0 }));
  return { user, taskPending: (taskStats as any).total_pending ?? 0 };
}

// Pixel icon: renders a colored grid from a pattern string
function PixelIcon({ pattern, colors, size = 14 }: {
  pattern: string[];
  colors: Record<string, string>;
  size?: number;
}) {
  const rows = pattern;
  const cols = rows[0].length;
  const px = Math.floor(size / cols);
  return (
    <div
      className="flex-shrink-0"
      style={{ width: size, height: size, display: "grid", gridTemplateColumns: `repeat(${cols}, ${px}px)`, gridTemplateRows: `repeat(${rows.length}, ${px}px)`, gap: 0 }}
    >
      {rows.map((row, r) =>
        row.split("").map((cell, c) =>
          cell === "." ? (
            <div key={`${r}-${c}`} style={{ width: px, height: px }} />
          ) : (
            <div key={`${r}-${c}`} style={{ width: px, height: px, backgroundColor: colors[cell] }} />
          )
        )
      )}
    </div>
  );
}

const ICONS = {
  chat: {
    pattern: [".BBBBB.","BWWWWWB","BWWWWWB","BWWWWWB","BWBBWWB",".BBBBB.","..BBB...","...B..."],
    colors: { B: "#00A3C4", W: "#CCF2FF" },
  },
  confirmations: {
    pattern: [".......","..YYY..",".YYYYY.",".YYYYY.",".YYYYY.",".YYYYY.","..YYY..","..YYY.."],
    colors: { Y: "#D69E2E" },
  },
  knowledgeMy: {
    pattern: [".YYYYY.",".YYYYYN",".YYYYNN",".YYYYYY",".YYYYYY",".YYYYYY",".YYYYYY",".YYYYYY"],
    colors: { Y: "#D69E2E", N: "#F6E05E" },
  },
  skills: {
    pattern: ["CC.....","..CC...","....CC.","......C","......C","....CC.","..CC...",".CC...."],
    colors: { C: "#00A3C4" },
  },
  data: {
    pattern: ["BBBBBBB","BWBWBWB","BBBBBBB","BWBWBWB","BBBBBBB","BWBWBWB","BBBBBBB","BWBWBWB"],
    colors: { B: "#3182CE", W: "#BEE3F8" },
  },
  intelSource: {
    pattern: [".TTTTT.","TTWWWTT","TW...WT","TW.T.WT","TW...WT","TTWWWTT",".TTTTT.",".......",],
    colors: { T: "#319795", W: "#B2F5EA" },
  },
  webApps: {
    pattern: ["PPP.PPP","PPP.PPP","PPP.PPP",".......","PPP.PPP","PPP.PPP","PPP.PPP","......."],
    colors: { P: "#D53F8C" },
  },
  intel: {
    pattern: [".......","..TTT..","TTWWWTT","TWWBBWT","TTWWWTT","..TTT..",".......","......."],
    colors: { T: "#319795", W: "#B2F5EA", B: "#1A202C" },
  },
  review: {
    pattern: [".......","......G",".....GG","....GG.","G..GG..","GGGG...",".GGG...","..G...."],
    colors: { G: "#38A169" },
  },
  skillsAdmin: {
    pattern: ["CC.....","..CC...","....CC.","......C","......C","....CC.","..CC...",".CC...."],
    colors: { C: "#00A3C4" },
  },
  models: {
    pattern: ["PPPPPPP","PWWWWWP","PWWWWWP","PWWWWWP","PPPPPPP","..PPP..",".PPPPP.","......."],
    colors: { P: "#805AD5", W: "#E9D8FD" },
  },
  bizTable: {
    pattern: [".BBBBB.","BBBBBBB","BWWWWWB",".BBBBB.","BWWWWWB","BWWWWWB",".BBBBB.","......."],
    colors: { B: "#2B6CB0", W: "#BEE3F8" },
  },
  tools: {
    pattern: ["..GGG..",".GGGGG.","GWWWWWG","GWWWWWG","GWWWWWG",".GGGGG.","..GGG...","......."],
    colors: { G: "#718096", W: "#E2E8F0" },
  },
  skillMarket: {
    pattern: ["CCCCCCC","CWWWWWC","CCCCCCC","CWWCWWC","CWWCWWC","CCCCCCC","CWWWWWC","CCCCCCC"],
    colors: { C: "#00A3C4", W: "#CCF2FF" },
  },
  mcpToken: {
    pattern: ["..CCCCC",".CWWWWC","CC....C","C.....C","CC....C",".CWWWWC","..CCCCC","......."],
    colors: { C: "#00A3C4", W: "#CCF2FF" },
  },
  intelAdmin: {
    pattern: ["TTTTTTT","TWWWWWT","TTTTTTT","TWWWWWT","TTTTTTT","TWWWWWT","TTTTTTT","......."],
    colors: { T: "#319795", W: "#B2F5EA" },
  },
  workspaceAdmin: {
    pattern: ["PPPPPPP","PWWWWWP","PPPPPPP","PWWWWWP","PPPPPPP",".......",".......",".......",],
    colors: { P: "#553C9A", W: "#E9D8FD" },
  },
  audit: {
    pattern: ["RRRRRRR","RWWWWWR","RWWWWWR","RRRRRRR","RWWWWWR","RWWWWWR","RRRRRRR","......."],
    colors: { R: "#C53030", W: "#FED7D7" },
  },
  contrib: {
    pattern: ["......G","....GGG","....GGG","..GGGGG","..GGGGG","GGGGGGG","GGGGGGG","GGGGGGG"],
    colors: { G: "#B7791F" },
  },
  users: {
    pattern: ["..CCC..","..CCC..",".CCCCC.","CCCCCCC","CWWWWWC","CWWWWWC","CCCCCCC","......."],
    colors: { C: "#00A3C4", W: "#CCF2FF" },
  },
  tasks: {
    pattern: ["GGGGGGG","G.....G","GWWWWWG","G.WWW.G","G..W..G","G.....G","GGGGGGG","......."],
    colors: { G: "#38A169", W: "#C6F6D5" },
  },
  approvals: {
    pattern: [".YYYYY.","Y.....Y","Y..G..Y","Y.GGG.Y","Y.G...Y","Y.....Y",".YYYYY.","......."],
    colors: { Y: "#D69E2E", G: "#38A169" },
  },
  skillPolicy: {
    pattern: ["GGGGGGG","G.....G","G.GGG.G","G.G.G.G","G.GGG.G","G.....G","GGGGGGG","......."],
    colors: { G: "#38A169" },
  },
  maskConfig: {
    pattern: ["PPPPPPP","P.....P","P.PPP.P","P.P.P.P","P.PPP.P","P.....P","PPPPPPP","......."],
    colors: { P: "#805AD5" },
  },
  outputSchema: {
    pattern: ["TTTTTTT","T.....T","T.TTT.T","T.T...T","T.TTT.T","T.....T","TTTTTTT","......."],
    colors: { T: "#319795" },
  },
  studioMetrics: {
    pattern: ["BBBBBBB","BWWWWWB","BWBBBWB","BWBBBWB","BWBBBWB","BWWWWWB","BBBBBBB","......."],
    colors: { B: "#2B6CB0", W: "#BEE3F8" },
  },
  // collapse toggle arrows
  chevronDown: {
    pattern: [".......",".......",".CCCCC.","..CCC..","...C...",".......",".......","......."],
    colors: { C: "#00A3C4" },
  },
  chevronRight: {
    pattern: [".C.....","..CC...","...CCC.","....CC.","...CCC.","..CC...","..C....",".......",],
    colors: { C: "#00A3C4" },
  },
};

function NavItem({ to, label, icon, collapsed }: {
  to: string;
  label: string;
  icon: { pattern: string[]; colors: Record<string, string> };
  collapsed: boolean;
}) {
  return (
    <NavLink
      to={to}
      title={collapsed ? label : undefined}
      className={({ isActive }) =>
        `flex items-center gap-2 px-2 py-2 text-xs font-bold uppercase tracking-wide transition-colors ${
          collapsed ? "justify-center" : ""
        } ${
          isActive
            ? "bg-[#CCF2FF] border-2 border-[#1A202C] text-[#1A202C]"
            : "text-[#1A202C] opacity-60 hover:opacity-100 hover:bg-white/50"
        }`
      }
    >
      <PixelIcon {...icon} size={14} />
      {!collapsed && <span>{label}</span>}
    </NavLink>
  );
}

function NavGroup({ label, children, storageKey, collapsed: sidebarCollapsed }: {
  label: string;
  children: React.ReactNode;
  storageKey: string;
  collapsed: boolean;
}) {
  const [open, setOpen] = useState(() => {
    if (typeof window === "undefined") return true;
    const saved = localStorage.getItem(storageKey);
    return saved === null ? true : saved === "true";
  });

  function toggle() {
    const next = !open;
    setOpen(next);
    localStorage.setItem(storageKey, String(next));
  }

  if (sidebarCollapsed) {
    // In collapsed mode just show the group divider (a thin cyan line)
    return (
      <div className="my-2">
        <div className="mx-2 border-t border-[#00A3C4]/40" />
        {children}
      </div>
    );
  }

  return (
    <div className="mt-1">
      <button
        onClick={toggle}
        className="w-full flex items-center justify-between px-3 pt-4 pb-1 text-[9px] font-bold text-[#00A3C4] uppercase tracking-widest hover:text-[#007A96] transition-colors"
      >
        <span>— {label}</span>
        <PixelIcon {...(open ? ICONS.chevronDown : ICONS.chevronRight)} size={8} />
      </button>
      {open && <div className="space-y-0.5">{children}</div>}
    </div>
  );
}

export default function AppLayout() {
  const { user, taskPending } = useLoaderData<typeof loader>() as { user: User; taskPending: number };
  const isAdmin = user.role === "super_admin" || user.role === "dept_admin";
  const isSuperAdmin = user.role === "super_admin";

  const [collapsed, setCollapsed] = useState(() => {
    if (typeof window === "undefined") return false;
    return localStorage.getItem("sidebar_collapsed") === "true";
  });

  function toggleSidebar() {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem("sidebar_collapsed", String(next));
  }

  return (
    <div className="flex h-screen bg-[#F0F4F8] overflow-hidden">
      {/* Sidebar */}
      <aside
        className={`flex-shrink-0 border-r-2 border-[#1A202C] bg-[#EBF4F7] flex flex-col justify-between transition-all duration-200 ${
          collapsed ? "w-14" : "w-56"
        }`}
      >
        <div className="overflow-y-auto flex-1">
          {/* Branding */}
          <div className={`border-b-2 border-[#1A202C] flex items-center ${collapsed ? "justify-center p-2" : "p-4 space-x-3"}`}>
            <div className="w-8 h-8 bg-[#00D1FF] pixel-border flex items-center justify-center flex-shrink-0">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
              </svg>
            </div>
            {!collapsed && (
              <div>
                <h1 className="text-xs font-bold uppercase tracking-tight leading-none">Universal</h1>
                <p className="text-[10px] text-[#00A3C4] font-bold uppercase mt-0.5">Knowledge Base</p>
              </div>
            )}
          </div>

          {/* Nav */}
          <nav className={`${collapsed ? "px-1 py-2" : "px-2 py-2"} space-y-0`}>

            {/* 工作台 */}
            <NavGroup label="工作台" storageKey="nav_group_workspace" collapsed={collapsed}>
              <NavItem to="/chat" label="对话" icon={ICONS.chat} collapsed={collapsed} />
              <NavItem to="/confirmations" label="待确认" icon={ICONS.confirmations} collapsed={collapsed} />
              <div className="relative">
                <NavItem to="/tasks" label="待办" icon={ICONS.tasks} collapsed={collapsed} />
                {taskPending > 0 && (
                  <span
                    className="absolute top-1 right-2 min-w-[16px] h-4 bg-red-500 text-white text-[8px] font-bold flex items-center justify-center px-1 pointer-events-none"
                    style={{ borderRadius: 0 }}
                  >
                    {taskPending > 99 ? "99+" : taskPending}
                  </span>
                )}
              </div>
            </NavGroup>

            {/* 知识管理 */}
            <NavGroup label="知识管理" storageKey="nav_group_knowledge" collapsed={collapsed}>
              <NavItem to="/knowledge/my" label="我的知识" icon={ICONS.knowledgeMy} collapsed={collapsed} />
              <NavItem to="/skills" label="Skill" icon={ICONS.skills} collapsed={collapsed} />
              <NavItem to="/data" label="数据表" icon={ICONS.data} collapsed={collapsed} />
              <NavItem to="/intel/sources" label="数据源" icon={ICONS.intelSource} collapsed={collapsed} />
            </NavGroup>

            {/* 工具 */}
            <NavGroup label="工具" storageKey="nav_group_tools" collapsed={collapsed}>
              <NavItem to="/web-apps" label="小工具" icon={ICONS.webApps} collapsed={collapsed} />
              <NavItem to="/intel" label="情报中心" icon={ICONS.intel} collapsed={collapsed} />
            </NavGroup>

            {/* 内容管理 */}
            {isAdmin && (
              <NavGroup label="内容管理" storageKey="nav_group_content" collapsed={collapsed}>
                <NavItem to="/admin/knowledge" label="知识审核" icon={ICONS.review} collapsed={collapsed} />
                <NavItem to="/admin/skills" label="Skill 管理" icon={ICONS.skillsAdmin} collapsed={collapsed} />
                <NavItem to="/admin/business-tables" label="业务表管理" icon={ICONS.bizTable} collapsed={collapsed} />
                <NavItem to="/admin/workspaces" label="工作台管理" icon={ICONS.workspaceAdmin} collapsed={collapsed} />
              </NavGroup>
            )}

            {/* AI 配置 */}
            {isAdmin && (
              <NavGroup label="AI 配置" storageKey="nav_group_ai" collapsed={collapsed}>
                <NavItem to="/admin/models" label="模型配置" icon={ICONS.models} collapsed={collapsed} />
                <NavItem to="/admin/tools" label="工具管理" icon={ICONS.tools} collapsed={collapsed} />
                <NavItem to="/admin/skill-market" label="外部市场" icon={ICONS.skillMarket} collapsed={collapsed} />
                <NavItem to="/admin/mcp-tokens" label="MCP Token" icon={ICONS.mcpToken} collapsed={collapsed} />
                <NavItem to="/admin/intel" label="情报管理" icon={ICONS.intelAdmin} collapsed={collapsed} />
              </NavGroup>
            )}

            {/* 权限安全 */}
            {isAdmin && (
              <NavGroup label="权限安全" storageKey="nav_group_permission" collapsed={collapsed}>
                <NavItem to="/admin/approvals" label="审批管理" icon={ICONS.approvals} collapsed={collapsed} />
                <NavItem to="/admin/skill-policies" label="Skill 策略" icon={ICONS.skillPolicy} collapsed={collapsed} />
                <NavItem to="/admin/mask-config" label="脱敏配置" icon={ICONS.maskConfig} collapsed={collapsed} />
                <NavItem to="/admin/output-schemas" label="输出 Schema" icon={ICONS.outputSchema} collapsed={collapsed} />
              </NavGroup>
            )}

            {/* 系统运营 */}
            {isAdmin && (
              <NavGroup label="系统运营" storageKey="nav_group_system" collapsed={collapsed}>
                <NavItem to="/admin/contributions" label="贡献排行" icon={ICONS.contrib} collapsed={collapsed} />
                {isSuperAdmin && (
                  <NavItem to="/admin/studio-metrics" label="Studio 监控" icon={ICONS.studioMetrics} collapsed={collapsed} />
                )}
                <NavItem to="/admin/audit" label="操作审计" icon={ICONS.audit} collapsed={collapsed} />
                {isSuperAdmin && (
                  <NavItem to="/admin/users" label="用户管理" icon={ICONS.users} collapsed={collapsed} />
                )}
              </NavGroup>
            )}
          </nav>
        </div>

        {/* User footer + collapse toggle */}
        <div className="border-t-2 border-[#1A202C] bg-white/40 flex-shrink-0">
          {!collapsed && (
            <div className="p-3 flex items-center space-x-2">
              <div className="w-7 h-7 bg-[#00CC99] pixel-border flex-shrink-0" />
              <div className="min-w-0">
                <div className="text-[10px] font-bold uppercase truncate">{user.display_name}</div>
                <div className="text-[9px] text-[#00A3C4] font-bold uppercase">
                  {user.role === "super_admin" ? "超管" : user.role === "dept_admin" ? "部门管理员" : "员工"}
                </div>
              </div>
            </div>
          )}

          <div className={`${collapsed ? "p-1" : "px-3 pb-3"} flex flex-col gap-1`}>
            {!collapsed && (
              <Form method="post" action="/logout">
                <button
                  type="submit"
                  className="w-full text-left px-2 py-1.5 text-[10px] font-bold uppercase tracking-wide text-gray-500 hover:bg-white/60 border border-transparent hover:border-gray-400 transition-colors"
                >
                  [退出登录]
                </button>
              </Form>
            )}
            {/* Collapse toggle button */}
            <button
              onClick={toggleSidebar}
              title={collapsed ? "展开侧边栏" : "收起侧边栏"}
              className={`${collapsed ? "mx-auto" : "ml-auto"} flex items-center justify-center w-8 h-8 border-2 border-[#1A202C] bg-white hover:bg-[#CCF2FF] transition-colors`}
            >
              <span className="text-[10px] font-bold text-[#1A202C]">{collapsed ? "»" : "«"}</span>
            </button>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto bg-[#F0F4F8]">
        <Outlet />
      </main>
    </div>
  );
}
