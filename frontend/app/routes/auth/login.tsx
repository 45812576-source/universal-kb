import { data, redirect } from "react-router";
import { Form, useActionData, useNavigation } from "react-router";
import type { Route } from "./+types/login";
import { apiFetch, ApiError } from "~/lib/api";
import { createUserSession, getSession } from "~/lib/auth.server";

export async function loader({ request }: Route.LoaderArgs) {
  const session = await getSession(request);
  if (session.get("token")) return redirect("/");
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
    <div className="flex min-h-screen items-center justify-center bg-[#F0F4F8]">
      <div className="w-full max-w-sm">
        {/* Header */}
        <div className="mb-6 text-center">
          <div className="inline-flex items-center justify-center w-12 h-12 bg-[#00D1FF] pixel-border mb-4">
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
            </svg>
          </div>
          <h1 className="text-sm font-bold uppercase tracking-widest text-[#1A202C]">Universal</h1>
          <p className="text-xs text-[#00A3C4] font-bold uppercase tracking-wider mt-0.5">Knowledge Base</p>
        </div>

        <div className="pixel-border bg-white p-6">
          {/* Terminal header */}
          <div className="bg-[#2D3748] text-white px-4 py-2 -mx-6 -mt-6 mb-6 flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-widest">Auth_Terminal</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>

          <Form method="post" className="space-y-4">
            <div>
              <label htmlFor="username" className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
                用户名
              </label>
              <input
                id="username"
                name="username"
                type="text"
                required
                autoFocus
                autoComplete="username"
                className="w-full border-2 border-[#1A202C] bg-[#F0F4F8] px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] focus:bg-[#CCF2FF]/20"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
                密码
              </label>
              <input
                id="password"
                name="password"
                type="password"
                required
                autoComplete="current-password"
                className="w-full border-2 border-[#1A202C] bg-[#F0F4F8] px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] focus:bg-[#CCF2FF]/20"
              />
            </div>

            {actionData?.error && (
              <div className="border-2 border-red-400 bg-red-50 px-3 py-2 text-xs font-bold text-red-600 uppercase">
                [ERROR] {actionData.error}
              </div>
            )}

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full bg-[#1A202C] text-white py-2.5 text-xs font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors mt-2"
            >
              {isSubmitting ? "登录中..." : "> 执行登录"}
            </button>
          </Form>
        </div>
      </div>
    </div>
  );
}
