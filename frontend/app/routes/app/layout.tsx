import { Form, NavLink, Outlet, useLoaderData } from "react-router";
import type { Route } from "./+types/layout";
import { requireUser } from "~/lib/auth.server";
import type { User } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { user } = await requireUser(request);
  return { user };
}

function NavItem({
  to,
  children,
}: {
  to: string;
  children: React.ReactNode;
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `block rounded-lg px-3 py-2 text-sm transition-colors ${
          isActive
            ? "bg-blue-50 text-blue-700 font-medium"
            : "text-gray-700 hover:bg-gray-100"
        }`
      }
    >
      {children}
    </NavLink>
  );
}

export default function AppLayout() {
  const { user } = useLoaderData<typeof loader>() as { user: User };
  const isAdmin =
    user.role === "super_admin" || user.role === "dept_admin";
  const isSuperAdmin = user.role === "super_admin";

  return (
    <div className="flex h-screen bg-gray-50">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 border-r border-gray-200 bg-white flex flex-col">
        {/* Header */}
        <div className="p-4 border-b border-gray-100">
          <h1 className="text-base font-bold text-gray-900">企业知识库</h1>
          <p className="text-xs text-gray-500 mt-0.5 truncate">{user.display_name}</p>
          <span className="inline-block mt-1 rounded px-1.5 py-0.5 text-xs bg-gray-100 text-gray-600">
            {user.role === "super_admin"
              ? "超级管理员"
              : user.role === "dept_admin"
              ? "部门管理员"
              : "员工"}
          </span>
        </div>

        {/* Navigation */}
        <nav className="flex-1 p-3 space-y-0.5">
          <p className="px-3 py-1.5 text-xs font-semibold text-gray-400 uppercase tracking-wide">
            工作台
          </p>
          <NavItem to="/chat">💬 对话</NavItem>

          <p className="px-3 py-1.5 mt-3 text-xs font-semibold text-gray-400 uppercase tracking-wide">
            知识贡献
          </p>
          <NavItem to="/knowledge/new">➕ 录入知识</NavItem>
          <NavItem to="/knowledge/my">📋 我的知识</NavItem>

          {isAdmin && (
            <>
              <p className="px-3 py-1.5 mt-3 text-xs font-semibold text-gray-400 uppercase tracking-wide">
                管理
              </p>
              <NavItem to="/admin/knowledge">📥 知识审核</NavItem>
              <NavItem to="/admin/skills">⚡ Skill管理</NavItem>
              <NavItem to="/admin/models">🤖 模型配置</NavItem>
              {isSuperAdmin && (
                <NavItem to="/admin/users">👥 用户管理</NavItem>
              )}
            </>
          )}
        </nav>

        {/* Logout */}
        <div className="p-3 border-t border-gray-100">
          <Form method="post" action="/logout">
            <button
              type="submit"
              className="w-full rounded-lg px-3 py-2 text-sm text-gray-500 hover:bg-gray-100 text-left transition-colors"
            >
              退出登录
            </button>
          </Form>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}
