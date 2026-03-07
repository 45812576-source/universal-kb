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
    // Chat
    layout("routes/app/chat/layout.tsx", [
      index("routes/app/chat/index.tsx"),
      route("chat/:id", "routes/app/chat/conversation.tsx"),
    ]),

    // Knowledge (employee)
    route("knowledge/new", "routes/app/knowledge/new.tsx"),
    route("knowledge/my", "routes/app/knowledge/my.tsx"),

    // Admin
    layout("routes/app/admin/layout.tsx", [
      route("admin/skills", "routes/app/admin/skills/index.tsx"),
      route("admin/skills/:id", "routes/app/admin/skills/detail.tsx"),
      route("admin/knowledge", "routes/app/admin/knowledge.tsx"),
      route("admin/models", "routes/app/admin/models.tsx"),
      route("admin/users", "routes/app/admin/users.tsx"),
    ]),
  ]),
] satisfies RouteConfig;
