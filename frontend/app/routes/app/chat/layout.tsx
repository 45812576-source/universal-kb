import { Form, Link, NavLink, Outlet, useLoaderData, useNavigation } from "react-router";
import type { Route } from "./+types/layout";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api.server";
import type { Conversation } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const conversations = await apiFetch("/api/conversations", { token });
  return { conversations };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const conv = await apiFetch("/api/conversations", { method: "POST", token });
  const { redirect } = await import("react-router");
  return redirect(`/chat/${conv.id}`);
}

export default function ChatLayout() {
  const { conversations } = useLoaderData<typeof loader>() as {
    conversations: Conversation[];
  };
  const navigation = useNavigation();

  return (
    <div className="flex h-full">
      {/* Conversation list */}
      <div className="w-60 flex-shrink-0 border-r border-gray-200 bg-gray-50 flex flex-col">
        <div className="p-3 border-b border-gray-200">
          <Form method="post">
            <button
              type="submit"
              disabled={navigation.state !== "idle"}
              className="w-full rounded-lg bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              + 新对话
            </button>
          </Form>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
          {conversations.length === 0 && (
            <p className="px-3 py-4 text-sm text-gray-400 text-center">
              还没有对话，点击上方新建
            </p>
          )}
          {conversations.map((c) => (
            <NavLink
              key={c.id}
              to={`/chat/${c.id}`}
              className={({ isActive }) =>
                `block rounded-lg px-3 py-2 text-sm truncate transition-colors ${
                  isActive
                    ? "bg-blue-50 text-blue-700 font-medium"
                    : "text-gray-700 hover:bg-gray-100"
                }`
              }
            >
              {c.title}
            </NavLink>
          ))}
        </div>
      </div>

      {/* Conversation content */}
      <div className="flex-1 overflow-hidden">
        <Outlet />
      </div>
    </div>
  );
}
