import {
  type RouteConfig,
  route,
  layout,
  index,
} from "@react-router/dev/routes";

export default [
  // Public
  route("login", "routes/auth/login.tsx"),
  route("logout", "routes/auth/logout.tsx"),

  // Authenticated app
  layout("routes/app/layout.tsx", [
    index("routes/app/home.tsx"),

    // Chat
    route("chat", "routes/app/chat/layout.tsx", [
      index("routes/app/chat/index.tsx"),
      route(":id", "routes/app/chat/conversation.tsx"),
    ]),

    // Confirmations
    route("confirmations", "routes/app/confirmations/index.tsx"),

    // Tasks (待办)
    route("tasks", "routes/app/tasks/index.tsx"),

    // Knowledge Management
    route("knowledge/my", "routes/app/knowledge/my.tsx"),
    // knowledge/new now redirects to knowledge/my (form is inline)
    route("knowledge/new", "routes/app/knowledge/new.tsx"),

    // Skills unified page
    route("skills", "routes/app/skills/index.tsx"),

    // Business data
    route("data", "routes/app/data/index.tsx"),
    route("data/:tableName", "routes/app/data/table.tsx"),

    // Intel
    route("intel", "routes/app/intel/index.tsx"),
    route("intel/sources", "routes/app/intel/sources.tsx"),

    // Web apps
    route("web-apps", "routes/app/web-apps/index.tsx"),

    // My workspaces
    route("workspaces/my", "routes/app/workspaces/my.tsx"),

    // Suggestions (kept for backwards compat, but removed from nav)
    route("suggestions/new", "routes/app/suggestions/new.tsx"),
    route("suggestions/my", "routes/app/suggestions/my.tsx"),

    // Admin
    layout("routes/app/admin/layout.tsx", [
      route("admin/skills", "routes/app/admin/skills/index.tsx"),
      route("admin/skills/:id", "routes/app/admin/skills/detail.tsx"),
      route("admin/knowledge", "routes/app/admin/knowledge.tsx"),
      route("admin/models", "routes/app/admin/models.tsx"),
      route("admin/users", "routes/app/admin/users.tsx"),
      route("admin/business-tables", "routes/app/admin/business-tables/index.tsx"),
      route("admin/business-tables/generate", "routes/app/admin/business-tables/generate.tsx"),
      route("admin/audit", "routes/app/admin/audit.tsx"),
      route("admin/contributions", "routes/app/admin/contributions.tsx"),
      route("admin/tools", "routes/app/admin/tools/index.tsx"),
      route("admin/intel", "routes/app/admin/intel/index.tsx"),
      route("admin/workspaces", "routes/app/admin/workspaces/index.tsx"),
      route("admin/workspaces/:id", "routes/app/admin/workspaces/detail.tsx"),
      route("admin/skill-market", "routes/app/admin/skill-market/index.tsx"),
      route("admin/mcp-tokens", "routes/app/admin/mcp-tokens/index.tsx"),
      route("admin/approvals", "routes/app/admin/approvals.tsx"),
      route("admin/skill-policies", "routes/app/admin/skill-policies.tsx"),
      route("admin/mask-config", "routes/app/admin/mask-config.tsx"),
      route("admin/output-schemas", "routes/app/admin/output-schemas.tsx"),
    ]),
  ]),
] satisfies RouteConfig;
