import { useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import type { Route } from "./+types/models";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";
import type { ModelConfig } from "~/lib/types";

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const models = await apiFetch("/api/admin/models", { token });
  return { models };
}

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const intent = form.get("intent") as string;
  const body = {
    name: form.get("name") as string,
    provider: form.get("provider") as string,
    model_id: form.get("model_id") as string,
    api_base: form.get("api_base") as string,
    api_key_env: form.get("api_key_env") as string,
    max_tokens: Number(form.get("max_tokens") || 4096),
    temperature: form.get("temperature") as string,
    is_default: form.get("is_default") === "true",
  };
  if (intent === "create") {
    await apiFetch("/api/admin/models", { method: "POST", body: JSON.stringify(body), token });
  } else if (intent === "update") {
    const id = form.get("id") as string;
    await apiFetch(`/api/admin/models/${id}`, { method: "PUT", body: JSON.stringify(body), token });
  } else if (intent === "delete") {
    const id = form.get("id") as string;
    await apiFetch(`/api/admin/models/${id}`, { method: "DELETE", token });
  }
  return null;
}

type FormState = {
  id?: number;
  name: string;
  provider: string;
  model_id: string;
  api_base: string;
  api_key_env: string;
  max_tokens: number;
  temperature: string;
  is_default: boolean;
};

const EMPTY_FORM: FormState = {
  name: "", provider: "deepseek", model_id: "deepseek-chat",
  api_base: "https://api.deepseek.com/v1", api_key_env: "DEEPSEEK_API_KEY",
  max_tokens: 4096, temperature: "0.7", is_default: false,
};

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">{children}</label>;
}

function PixelInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${props.className || ""}`} />;
}

export default function ModelsPage() {
  const { models } = useLoaderData<typeof loader>() as { models: ModelConfig[] };
  const fetcher = useFetcher();
  const [editing, setEditing] = useState<FormState | null>(null);
  const isSubmitting = fetcher.state !== "idle";

  const openNew = () => setEditing({ ...EMPTY_FORM });
  const openEdit = (m: ModelConfig) => setEditing({
    id: m.id, name: m.name, provider: m.provider, model_id: m.model_id,
    api_base: m.api_base, api_key_env: m.api_key_env, max_tokens: m.max_tokens,
    temperature: m.temperature, is_default: m.is_default,
  });

  const handleDelete = (id: number) => {
    if (!confirm("确定删除此模型配置？")) return;
    const fd = new FormData();
    fd.set("intent", "delete");
    fd.set("id", String(id));
    fetcher.submit(fd, { method: "post" });
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!editing) return;
    const fd = new FormData(e.currentTarget);
    fd.set("intent", editing.id ? "update" : "create");
    fd.set("is_default", fd.get("is_default_check") === "on" ? "true" : "false");
    if (editing.id) fd.set("id", String(editing.id));
    fetcher.submit(fd, { method: "post" });
    setEditing(null);
  };

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-1.5 h-5 bg-[#00D1FF]" />
          <div>
            <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">模型配置</h1>
            <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">管理 LLM 接入配置，支持 OpenAI 兼容接口</p>
          </div>
        </div>
        <button
          onClick={openNew}
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black pixel-border transition-colors"
        >
          + 新增模型
        </button>
      </div>

      <div className="p-6 max-w-4xl">
        <div className="pixel-border bg-white overflow-hidden">
          <div className="bg-[#2D3748] text-white px-4 py-2.5 flex items-center justify-between border-b-2 border-[#1A202C]">
            <span className="text-[10px] font-bold uppercase tracking-widest">Model_Registry</span>
            <div className="flex space-x-1.5">
              <div className="w-2 h-2 bg-red-400" />
              <div className="w-2 h-2 bg-yellow-400" />
              <div className="w-2 h-2 bg-green-400" />
            </div>
          </div>
          <table className="w-full text-left">
            <thead>
              <tr className="border-b-2 border-[#1A202C] bg-[#F0F4F8]">
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">名称</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">提供商</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">模型ID</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">API Base</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500">默认</th>
                <th className="py-3 px-4 text-[10px] font-bold uppercase tracking-widest text-gray-500 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {models.map((m) => (
                <tr key={m.id} className="hover:bg-[#F0F4F8] transition-colors">
                  <td className="py-3 px-4 text-xs font-bold text-[#1A202C]">{m.name}</td>
                  <td className="py-3 px-4 text-[10px] font-bold uppercase text-gray-500">{m.provider}</td>
                  <td className="py-3 px-4 text-[10px] font-bold font-mono text-gray-500">{m.model_id}</td>
                  <td className="py-3 px-4 text-[10px] text-gray-400 truncate max-w-[160px] font-mono">{m.api_base}</td>
                  <td className="py-3 px-4">
                    {m.is_default && (
                      <span className="inline-block border border-[#00D1FF] px-2 py-0.5 text-[9px] font-bold uppercase bg-[#CCF2FF] text-[#00A3C4]">
                        默认
                      </span>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center justify-end gap-3">
                      <button onClick={() => openEdit(m)} className="text-[10px] font-bold uppercase text-[#00A3C4] hover:underline">
                        编辑
                      </button>
                      <button onClick={() => handleDelete(m.id)} className="text-[10px] font-bold uppercase text-red-500 hover:underline">
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {models.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-xs font-bold uppercase text-gray-400">
                    暂无模型配置 — 点击右上角新增
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Edit/Create modal */}
      {editing && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="pixel-border bg-white w-full max-w-lg mx-4">
            <div className="bg-[#2D3748] text-white px-4 py-2.5 border-b-2 border-[#1A202C] flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-widest">
                {editing.id ? "编辑模型配置" : "新增模型配置"}
              </span>
              <div className="flex space-x-1.5">
                <div className="w-2 h-2 bg-red-400" />
                <div className="w-2 h-2 bg-yellow-400" />
                <div className="w-2 h-2 bg-green-400" />
              </div>
            </div>
            <form onSubmit={handleSubmit} className="p-6 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <FieldLabel>名称 <span className="text-[#00D1FF]">*</span></FieldLabel>
                  <PixelInput name="name" required defaultValue={editing.name} placeholder="DeepSeek V3" />
                </div>
                <div>
                  <FieldLabel>提供商</FieldLabel>
                  <PixelInput name="provider" defaultValue={editing.provider} placeholder="deepseek / openai" />
                </div>
              </div>
              <div>
                <FieldLabel>模型ID <span className="text-[#00D1FF]">*</span></FieldLabel>
                <PixelInput name="model_id" required defaultValue={editing.model_id} placeholder="deepseek-chat" className="font-mono" />
              </div>
              <div>
                <FieldLabel>API Base URL <span className="text-[#00D1FF]">*</span></FieldLabel>
                <PixelInput name="api_base" required defaultValue={editing.api_base} placeholder="https://api.deepseek.com/v1" className="font-mono" />
              </div>
              <div>
                <FieldLabel>API Key 环境变量名</FieldLabel>
                <PixelInput name="api_key_env" defaultValue={editing.api_key_env} placeholder="DEEPSEEK_API_KEY" className="font-mono" />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <FieldLabel>Max Tokens</FieldLabel>
                  <PixelInput name="max_tokens" type="number" defaultValue={editing.max_tokens} />
                </div>
                <div>
                  <FieldLabel>Temperature</FieldLabel>
                  <PixelInput name="temperature" defaultValue={editing.temperature} placeholder="0.7" />
                </div>
              </div>
              <label className="flex items-center gap-2 text-xs font-bold uppercase text-gray-700 cursor-pointer">
                <input type="checkbox" name="is_default_check" defaultChecked={editing.is_default} className="border-2 border-[#1A202C] w-4 h-4" />
                设为默认模型
              </label>
              <div className="flex gap-3 pt-2">
                <button
                  type="submit"
                  disabled={isSubmitting}
                  className="bg-[#1A202C] text-white px-5 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border"
                >
                  {isSubmitting ? "保存中..." : "保存"}
                </button>
                <button
                  type="button"
                  onClick={() => setEditing(null)}
                  className="border-2 border-[#1A202C] bg-white px-5 py-2 text-[10px] font-bold uppercase text-gray-600 hover:bg-gray-100"
                >
                  取消
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
