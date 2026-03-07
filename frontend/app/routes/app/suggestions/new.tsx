import { redirect } from "react-router";
import { Form, useActionData, useLoaderData, useNavigation } from "react-router";
import type { Route } from "./+types/new";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const url = new URL(request.url);
  const skill_id = url.searchParams.get("skill_id") ?? "";
  const skills = await apiFetch("/api/skills?status=published", { token });
  return { skills, skill_id, token };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const skill_id = Number(form.get("skill_id"));
  const body = {
    problem_desc: form.get("problem_desc") as string,
    expected_direction: form.get("expected_direction") as string,
    case_example: (form.get("case_example") as string) || undefined,
  };
  try {
    await apiFetch(`/api/skills/${skill_id}/suggestions`, {
      method: "POST",
      body: JSON.stringify(body),
      token,
    });
    return redirect("/suggestions/my");
  } catch (e) {
    if (e instanceof ApiError) return { error: e.message };
    return { error: "提交失败" };
  }
}

function FieldLabel({ children, note }: { children: React.ReactNode; note?: string }) {
  return (
    <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
      {children}
      {note && <span className="text-gray-400 font-bold ml-1 normal-case">({note})</span>}
    </label>
  );
}

export default function NewSuggestionPage() {
  const { skills, skill_id } = useLoaderData<typeof loader>() as any;
  const actionData = useActionData<typeof action>() as any;
  const navigation = useNavigation();
  const isSubmitting = navigation.state !== "idle";

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
        <div className="w-1.5 h-5 bg-[#00D1FF]" />
        <div>
          <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">提交 Skill 改进建议</h1>
          <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">优秀建议将被采纳并更新到下一版本</p>
        </div>
      </div>

      <div className="p-6 max-w-2xl">
        {actionData?.error && (
          <div className="mb-4 border-2 border-red-400 bg-red-50 px-4 py-3 text-xs font-bold text-red-700 uppercase">
            [ERROR] {actionData.error}
          </div>
        )}

        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-widest">Suggestion_Form</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>

          <Form method="post" className="p-5 space-y-4">
            <div>
              <FieldLabel>选择 Skill <span className="text-[#00D1FF]">*</span></FieldLabel>
              <select
                name="skill_id"
                required
                defaultValue={skill_id}
                className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF]"
              >
                <option value="">请选择...</option>
                {skills?.map((s: any) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </div>

            <div>
              <FieldLabel>问题描述 <span className="text-[#00D1FF]">*</span></FieldLabel>
              <textarea
                name="problem_desc"
                required
                rows={3}
                placeholder="这个Skill在什么情况下表现不好？具体哪里有问题？"
                className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-none"
              />
            </div>

            <div>
              <FieldLabel>期望改进方向 <span className="text-[#00D1FF]">*</span></FieldLabel>
              <textarea
                name="expected_direction"
                required
                rows={3}
                placeholder="你希望它怎么改进？有什么具体的方向或建议？"
                className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-none"
              />
            </div>

            <div>
              <FieldLabel note="可选">实际案例</FieldLabel>
              <textarea
                name="case_example"
                rows={3}
                placeholder="粘贴一个典型的对话或输出案例..."
                className="w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-none"
              />
            </div>

            <div className="flex gap-3 pt-2">
              <button
                type="submit"
                disabled={isSubmitting}
                className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
              >
                {isSubmitting ? "提交中..." : "> 提交建议"}
              </button>
              <a
                href="/suggestions/my"
                className="border-2 border-[#1A202C] bg-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest text-gray-600 hover:bg-gray-100 transition-colors"
              >
                我的建议
              </a>
            </div>
          </Form>
        </div>
      </div>
    </div>
  );
}
