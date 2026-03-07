import { data, redirect } from "react-router";
import { Form, useActionData, useNavigation } from "react-router";
import type { Route } from "./+types/login";
import { apiFetch, ApiError } from "~/lib/api.server";
import { createUserSession, getSession } from "~/lib/auth.server";

export async function loader({ request }: Route.LoaderArgs) {
  const session = await getSession(request);
  if (session.get("token")) return redirect("/chat");
  return null;
}

export async function action({ request }: Route.ActionArgs) {
  const form = await request.formData();
  const username = form.get("username") as string;
  const password = form.get("password") as string;

  try {
    const result = await apiFetch("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    return createUserSession(result.access_token, result.user);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      return data({ error: "用户名或密码错误" }, { status: 401 });
    }
    return data({ error: "登录失败，请稍后再试" }, { status: 500 });
  }
}

export default function Login() {
  const actionData = useActionData<typeof action>();
  const navigation = useNavigation();
  const isSubmitting = navigation.state === "submitting";

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm rounded-xl bg-white p-8 shadow-md">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-bold text-gray-900">企业知识库</h1>
          <p className="mt-1 text-sm text-gray-500">请登录以继续</p>
        </div>

        <Form method="post" className="space-y-4">
          <div>
            <label htmlFor="username" className="block text-sm font-medium text-gray-700 mb-1">
              用户名
            </label>
            <input
              id="username"
              name="username"
              type="text"
              required
              autoFocus
              autoComplete="username"
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm font-medium text-gray-700 mb-1">
              密码
            </label>
            <input
              id="password"
              name="password"
              type="password"
              required
              autoComplete="current-password"
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          {actionData?.error && (
            <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600">
              {actionData.error}
            </p>
          )}

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full rounded-lg bg-blue-600 py-2.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {isSubmitting ? "登录中..." : "登录"}
          </button>
        </Form>
      </div>
    </div>
  );
}
