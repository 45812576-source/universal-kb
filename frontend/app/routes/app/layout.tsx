import { Form, NavLink, Outlet, useLoaderData } from "react-router";
import type { Route } from "./+types/layout";
import { requireUser } from "~/lib/auth.server";
import type { User } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { user } = await requireUser(request);
  return { user };
}

// Pixel icon: renders a colored grid from a pattern string
// pattern: "." = empty, any letter = color key
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
      className="flex-shrink-0 mr-2"
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

// Individual pixel icons
const ICONS = {
  // 对话: cyan speech bubble
  chat: {
    pattern: [
      ".BBBBB.",
      "BWWWWWB",
      "BWWWWWB",
      "BWWWWWB",
      "BWBBWWB",
      ".BBBBB.",
      "..BBB..",
      "...B...",
    ],
    colors: { B: "#00A3C4", W: "#CCF2FF" },
  },
  // 待确认: yellow bell
  confirmations: {
    pattern: [
      ".......",
      "..YYY..",
      ".YYYYY.",
      ".YYYYY.",
      ".YYYYY.",
      ".YYYYY.",
      "..YYY..",
      "..YYY..",
    ],
    colors: { Y: "#D69E2E" },
  },
  // 我的工作台: purple layers
  workspace: {
    pattern: [
      ".PPPPP.",
      "PPPPPPP",
      ".PPPPP.",
      "..PPP..",
      ".PPPPP.",
      "PPPPPPP",
      ".PPPPP.",
      "..PPP..",
    ],
    colors: { P: "#9F7AEA" },
  },
  // 录入知识: green plus
  knowledgeNew: {
    pattern: [
      "..GGG..",
      "..GGG..",
      "GGGGGGG",
      "GGGGGGG",
      "GGGGGGG",
      "..GGG..",
      "..GGG..",
      "..GGG..",
    ],
    colors: { G: "#38A169" },
  },
  // 我的知识: yellow document
  knowledgeMy: {
    pattern: [
      ".YYYYY.",
      ".YYYYYN",
      ".YYYYNN",
      ".YYYYYY",
      ".YYYYYY",
      ".YYYYYY",
      ".YYYYYY",
      ".YYYYYY",
    ],
    colors: { Y: "#D69E2E", N: "#F6E05E" },
  },
  // 数据表: blue grid
  data: {
    pattern: [
      "BBBBBBB",
      "BWBWBWB",
      "BBBBBBB",
      "BWBWBWB",
      "BBBBBBB",
      "BWBWBWB",
      "BBBBBBB",
      "BWBWBWB",
    ],
    colors: { B: "#3182CE", W: "#BEE3F8" },
  },
  // 提交意见: orange chat
  suggestionNew: {
    pattern: [
      "OOOOOOO",
      "OWWWWWO",
      "OWWWWWO",
      "OWWWWWO",
      "OOOOOOO",
      "..OO...",
      "...OO..",
      "....OO.",
    ],
    colors: { O: "#DD6B20", W: "#FEEBC8" },
  },
  // 我的意见: orange clipboard
  suggestionMy: {
    pattern: [
      "..OOO..",
      ".OOOOO.",
      "OOOOOOO",
      "OWWWWWO",
      "OWWWWWO",
      "OWWWWWO",
      "OWWWWWO",
      "OOOOOOO",
    ],
    colors: { O: "#C05621", W: "#FEEBC8" },
  },
  // 小工具: pink 2x2 grid
  webApps: {
    pattern: [
      "PPP.PPP",
      "PPP.PPP",
      "PPP.PPP",
      ".......",
      "PPP.PPP",
      "PPP.PPP",
      "PPP.PPP",
      ".......",
    ],
    colors: { P: "#D53F8C" },
  },
  // 情报中心: teal eye
  intel: {
    pattern: [
      ".......",
      ".TTTTT.",
      "TTWWWTT",
      "TWWBBWT",
      "TTWWWTT",
      ".TTTTT.",
      ".......",
      ".......",
    ],
    colors: { T: "#319795", W: "#B2F5EA", B: "#1A202C" },
  },
  // 知识审核: green checkmark
  review: {
    pattern: [
      ".......",
      "......G",
      ".....GG",
      "....GG.",
      "G..GG..",
      "GGGG...",
      ".GGG...",
      "..G....",
    ],
    colors: { G: "#38A169" },
  },
  // Skill管理: cyan code brackets
  skills: {
    pattern: [
      "CC.....",
      ".CC....",
      "..CC...",
      "...CC..",
      "...CC..",
      "..CC...",
      ".CC....",
      "CC.....",
    ],
    colors: { C: "#00A3C4" },
  },
  // 模型配置: purple monitor
  models: {
    pattern: [
      "PPPPPPP",
      "PWWWWWP",
      "PWWWWWP",
      "PWWWWWP",
      "PPPPPPP",
      "..PPP..",
      ".PPPPP.",
      ".......",
    ],
    colors: { P: "#805AD5", W: "#E9D8FD" },
  },
  // 业务表管理: blue database
  bizTable: {
    pattern: [
      ".BBBBB.",
      "BBBBBBB",
      "BWWWWWB",
      ".BBBBB.",
      "BWWWWWB",
      "BWWWWWB",
      ".BBBBB.",
      ".......",
    ],
    colors: { B: "#2B6CB0", W: "#BEE3F8" },
  },
  // 工具管理: gray gear
  tools: {
    pattern: [
      "..GGG..",
      ".GGGGG.",
      "GWWWWWG",
      "GWWWWWG",
      "GWWWWWG",
      ".GGGGG.",
      "..GGG..",
      ".......",
    ],
    colors: { G: "#718096", W: "#E2E8F0" },
  },
  // 情报管理: teal newspaper
  intelAdmin: {
    pattern: [
      "TTTTTTT",
      "TWWWWWT",
      "TTTTTTT",
      "TWWWWWT",
      "TTTTTTT",
      "TWWWWWT",
      "TTTTTTT",
      ".......",
    ],
    colors: { T: "#319795", W: "#B2F5EA" },
  },
  // 外部市场: cyan store
  skillMarket: {
    pattern: [
      "CCCCCCC",
      "CWWWWWC",
      "CCCCCCC",
      "CWWCWWC",
      "CWWCWWC",
      "CCCCCCC",
      "CWWWWWC",
      "CCCCCCC",
    ],
    colors: { C: "#00A3C4", W: "#CCF2FF" },
  },
  // MCP Token: cyan key
  mcpToken: {
    pattern: [
      "..CCCCC",
      ".CWWWWC",
      "CC....C",
      "C.....C",
      "CC....C",
      ".CWWWWC",
      "..CCCCC",
      ".......",
    ],
    colors: { C: "#00A3C4", W: "#CCF2FF" },
  },
  // 工作台管理: purple layers admin
  workspaceAdmin: {
    pattern: [
      "PPPPPPP",
      "PWWWWWP",
      "PPPPPPP",
      "PWWWWWP",
      "PPPPPPP",
      ".......",
      ".......",
      ".......",
    ],
    colors: { P: "#553C9A", W: "#E9D8FD" },
  },
  // 操作审计: red list
  audit: {
    pattern: [
      "RRRRRRR",
      "RWWWWWR",
      "RWWWWWR",
      "RRRRRRR",
      "RWWWWWR",
      "RWWWWWR",
      "RRRRRRR",
      ".......",
    ],
    colors: { R: "#C53030", W: "#FED7D7" },
  },
  // 贡献排行: gold bars
  contrib: {
    pattern: [
      "......G",
      "....GGG",
      "....GGG",
      "..GGGGG",
      "..GGGGG",
      "GGGGGGG",
      "GGGGGGG",
      "GGGGGGG",
    ],
    colors: { G: "#B7791F" },
  },
  // 用户管理: cyan person
  users: {
    pattern: [
      "..CCC..",
      "..CCC..",
      ".CCCCC.",
      "CCCCCCC",
      "CWWWWWC",
      "CWWWWWC",
      "CCCCCCC",
      ".......",
    ],
    colors: { C: "#00A3C4", W: "#CCF2FF" },
  },
};

function NavItem({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `flex items-center px-3 py-2 text-xs font-bold uppercase tracking-wide transition-colors ${
          isActive
            ? "bg-[#CCF2FF] border-2 border-[#1A202C] text-[#1A202C]"
            : "text-[#1A202C] opacity-60 hover:opacity-100 hover:bg-white/50"
        }`
      }
    >
      {children}
    </NavLink>
  );
}

function NavSection({ label }: { label: string }) {
  return (
    <p className="px-3 pt-4 pb-1 text-[9px] font-bold text-[#00A3C4] uppercase tracking-widest">
      — {label}
    </p>
  );
}

export default function AppLayout() {
  const { user } = useLoaderData<typeof loader>() as { user: User };
  const isAdmin = user.role === "super_admin" || user.role === "dept_admin";
  const isSuperAdmin = user.role === "super_admin";

  return (
    <div className="flex h-screen bg-[#F0F4F8] overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 border-r-2 border-[#1A202C] bg-[#EBF4F7] flex flex-col justify-between">
        <div className="overflow-y-auto">
          {/* Branding */}
          <div className="p-5 flex items-center space-x-3 border-b-2 border-[#1A202C]">
            <div className="w-9 h-9 bg-[#00D1FF] pixel-border flex items-center justify-center flex-shrink-0">
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
              </svg>
            </div>
            <div>
              <h1 className="text-xs font-bold uppercase tracking-tight leading-none">Universal</h1>
              <p className="text-[10px] text-[#00A3C4] font-bold uppercase mt-0.5">Knowledge Base</p>
            </div>
          </div>

          {/* Nav */}
          <nav className="p-3 space-y-0.5">
            <NavSection label="工作台" />
            <NavItem to="/chat">
              <PixelIcon {...ICONS.chat} />
              对话
            </NavItem>
            <NavItem to="/confirmations">
              <PixelIcon {...ICONS.confirmations} />
              待确认
            </NavItem>

            <NavSection label="知识贡献" />
            <NavItem to="/knowledge/new">
              <PixelIcon {...ICONS.knowledgeNew} />
              录入知识
            </NavItem>
            <NavItem to="/knowledge/my">
              <PixelIcon {...ICONS.knowledgeMy} />
              我的知识
            </NavItem>

            <NavSection label="业务数据" />
            <NavItem to="/data">
              <PixelIcon {...ICONS.data} />
              数据表
            </NavItem>

            <NavSection label="Skill 反馈" />
            <NavItem to="/suggestions/new">
              <PixelIcon {...ICONS.suggestionNew} />
              提交意见
            </NavItem>
            <NavItem to="/suggestions/my">
              <PixelIcon {...ICONS.suggestionMy} />
              我的意见
            </NavItem>

            <NavSection label="工具" />
            <NavItem to="/web-apps">
              <PixelIcon {...ICONS.webApps} />
              小工具
            </NavItem>
            <NavItem to="/intel">
              <PixelIcon {...ICONS.intel} />
              情报中心
            </NavItem>

            {isAdmin && (
              <>
                <NavSection label="管理" />
                <NavItem to="/admin/knowledge">
                  <PixelIcon {...ICONS.review} />
                  知识审核
                </NavItem>
                <NavItem to="/admin/skills">
                  <PixelIcon {...ICONS.skills} />
                  Skill管理
                </NavItem>
                <NavItem to="/admin/models">
                  <PixelIcon {...ICONS.models} />
                  模型配置
                </NavItem>
                <NavItem to="/admin/business-tables">
                  <PixelIcon {...ICONS.bizTable} />
                  业务表管理
                </NavItem>
                <NavItem to="/admin/tools">
                  <PixelIcon {...ICONS.tools} />
                  工具管理
                </NavItem>
                <NavItem to="/admin/skill-market">
                  <PixelIcon {...ICONS.skillMarket} />
                  外部市场
                </NavItem>
                <NavItem to="/admin/mcp-tokens">
                  <PixelIcon {...ICONS.mcpToken} />
                  MCP Token
                </NavItem>
                <NavItem to="/admin/intel">
                  <PixelIcon {...ICONS.intelAdmin} />
                  情报管理
                </NavItem>
                <NavItem to="/admin/workspaces">
                  <PixelIcon {...ICONS.workspaceAdmin} />
                  工作台管理
                </NavItem>
                <NavItem to="/admin/audit">
                  <PixelIcon {...ICONS.audit} />
                  操作审计
                </NavItem>
                <NavItem to="/admin/contributions">
                  <PixelIcon {...ICONS.contrib} />
                  贡献排行
                </NavItem>
                {isSuperAdmin && (
                  <NavItem to="/admin/users">
                    <PixelIcon {...ICONS.users} />
                    用户管理
                  </NavItem>
                )}
              </>
            )}
          </nav>
        </div>

        {/* User footer */}
        <div className="p-4 border-t-2 border-[#1A202C] bg-white/40 flex-shrink-0">
          <div className="flex items-center space-x-3 mb-3">
            <div className="w-8 h-8 bg-[#00CC99] pixel-border flex-shrink-0" />
            <div>
              <div className="text-[10px] font-bold uppercase truncate max-w-[100px]">{user.display_name}</div>
              <div className="text-[9px] text-[#00A3C4] font-bold uppercase">
                {user.role === "super_admin" ? "超级管理员" : user.role === "dept_admin" ? "部门管理员" : "员工"}
              </div>
            </div>
          </div>
          <Form method="post" action="/logout">
            <button
              type="submit"
              className="w-full text-left px-3 py-1.5 text-[10px] font-bold uppercase tracking-wide text-gray-500 hover:bg-white/60 border border-transparent hover:border-gray-400 transition-colors"
            >
              [退出登录]
            </button>
          </Form>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto bg-[#F0F4F8]">
        <Outlet />
      </main>
    </div>
  );
}
