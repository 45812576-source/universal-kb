import { useState } from "react";
import { data, redirect, useActionData, useNavigation } from "react-router";
import { Form } from "react-router";
import type { Route } from "./+types/new";
import { requireUser } from "~/lib/auth.server";
import { apiFetch, ApiError } from "~/lib/api";

export async function action({ request }: Route.ActionArgs) {
  const { token } = await requireUser(request);
  const form = await request.formData();
  const mode = form.get("mode") as string;

  const tagsFromField = (field: string) =>
    (form.get(field) as string)
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

  try {
    if (mode === "text") {
      const result = await apiFetch("/api/knowledge", {
        method: "POST",
        body: JSON.stringify({
          title: form.get("title") as string,
          content: form.get("content") as string,
          category: form.get("category") as string,
          industry_tags: tagsFromField("industry_tags"),
          platform_tags: tagsFromField("platform_tags"),
          topic_tags: tagsFromField("topic_tags"),
        }),
        token,
      });
      return redirect(`/knowledge/my?submitted=${result.id}`);
    } else {
      const uploadForm = new FormData();
      uploadForm.set("title", form.get("title") as string);
      uploadForm.set("category", form.get("category") as string);
      uploadForm.set("industry_tags", JSON.stringify(tagsFromField("industry_tags")));
      uploadForm.set("platform_tags", JSON.stringify(tagsFromField("platform_tags")));
      uploadForm.set("topic_tags", JSON.stringify(tagsFromField("topic_tags")));
      const file = form.get("file") as File;
      uploadForm.set("file", file);
      const result = await apiFetch("/api/knowledge/upload", {
        method: "POST",
        body: uploadForm,
        token,
      });
      return redirect(`/knowledge/my?submitted=${result.id}`);
    }
  } catch (e) {
    if (e instanceof ApiError) {
      return data({ error: e.message }, { status: e.status });
    }
    return data({ error: "提交失败，请重试" }, { status: 500 });
  }
}

const CATEGORIES = [
  { value: "experience", label: "经验总结" },
  { value: "methodology", label: "方法论" },
  { value: "case_study", label: "案例" },
  { value: "data", label: "数据资产" },
  { value: "template", label: "模板" },
  { value: "external", label: "外部资料" },
];

function PageHeader() {
  return (
    <div className="border-b-2 border-[#1A202C] bg-[#EBF4F7] px-6 py-4 flex items-center gap-4">
      <div className="w-1.5 h-5 bg-[#00D1FF]" />
      <div>
        <h1 className="text-xs font-bold uppercase tracking-widest text-[#1A202C]">录入知识</h1>
        <p className="text-[9px] text-gray-500 uppercase font-bold mt-0.5">提交后进入审核队列</p>
      </div>
    </div>
  );
}

function FieldLabel({ children, required }: { children: React.ReactNode; required?: boolean }) {
  return (
    <label className="block text-[10px] font-bold uppercase tracking-widest text-gray-500 mb-1.5">
      {children}{required && <span className="text-[#00D1FF] ml-1">*</span>}
    </label>
  );
}

function PixelInput({ className, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${className || ""}`}
    />
  );
}

function PixelSelect({ className, children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={`w-full border-2 border-[#1A202C] bg-white px-3 py-2 text-xs font-bold focus:outline-none focus:border-[#00D1FF] ${className || ""}`}
    >
      {children}
    </select>
  );
}

export default function NewKnowledge() {
  const actionData = useActionData<typeof action>() as any;
  const navigation = useNavigation();
  const [mode, setMode] = useState<"text" | "file">("text");
  const isSubmitting = navigation.state !== "idle";

  return (
    <div className="min-h-full bg-[#F0F4F8]">
      <PageHeader />
      <div className="p-6 max-w-2xl">
        {actionData?.error && (
          <div className="mb-4 border-2 border-red-400 bg-red-50 px-4 py-3 text-xs font-bold text-red-700 uppercase">
            [ERROR] {actionData.error}
          </div>
        )}

        {/* Mode toggle */}
        <div className="flex border-2 border-[#1A202C] mb-6 w-fit">
          <button
            type="button"
            onClick={() => setMode("text")}
            className={`px-5 py-2 text-[10px] font-bold uppercase tracking-widest transition-colors ${
              mode === "text" ? "bg-[#1A202C] text-white" : "bg-white text-gray-600 hover:bg-gray-100"
            }`}
          >
            文字录入
          </button>
          <button
            type="button"
            onClick={() => setMode("file")}
            className={`px-5 py-2 text-[10px] font-bold uppercase tracking-widest border-l-2 border-[#1A202C] transition-colors ${
              mode === "file" ? "bg-[#1A202C] text-white" : "bg-white text-gray-600 hover:bg-gray-100"
            }`}
          >
            文件上传
          </button>
        </div>

        <Form
          method="post"
          encType={mode === "file" ? "multipart/form-data" : undefined}
          className="space-y-5"
        >
          <input type="hidden" name="mode" value={mode} />

          <div>
            <FieldLabel required>标题</FieldLabel>
            <PixelInput name="title" required placeholder="例: 618大促投放ROI提升方法论" />
          </div>

          <div>
            <FieldLabel>分类</FieldLabel>
            <PixelSelect name="category">
              {CATEGORIES.map((c) => (
                <option key={c.value} value={c.value}>{c.label}</option>
              ))}
            </PixelSelect>
          </div>

          {mode === "text" ? (
            <div>
              <FieldLabel required>内容</FieldLabel>
              <textarea
                name="content"
                required
                rows={10}
                className="w-full border-2 border-[#1A202C] bg-white px-3 py-2.5 text-xs font-bold focus:outline-none focus:border-[#00D1FF] resize-y"
                placeholder="请详细描述你的经验、方法或案例..."
              />
            </div>
          ) : (
            <div>
              <FieldLabel required>文件</FieldLabel>
              <div className="border-2 border-dashed border-[#1A202C] px-6 py-8 text-center bg-white">
                <p className="text-[10px] font-bold uppercase text-gray-400 mb-3">
                  支持 PDF / DOCX / PPTX / MD / TXT
                </p>
                <input
                  type="file"
                  name="file"
                  required
                  accept=".pdf,.docx,.pptx,.md,.txt"
                  className="block w-full text-xs font-bold text-gray-500 file:mr-3 file:py-1.5 file:px-4 file:border-2 file:border-[#1A202C] file:bg-[#CCF2FF] file:text-xs file:font-bold file:uppercase cursor-pointer"
                />
              </div>
            </div>
          )}

          {/* Tags */}
          <div className="border-2 border-[#1A202C] bg-[#EBF4F7] p-4 space-y-3">
            <p className="text-[9px] font-bold text-[#00A3C4] uppercase tracking-widest">
              — 标签（逗号分隔，可选）
            </p>
            <div>
              <FieldLabel>行业标签</FieldLabel>
              <PixelInput name="industry_tags" placeholder="电商, 快消, 汽车" />
            </div>
            <div>
              <FieldLabel>平台标签</FieldLabel>
              <PixelInput name="platform_tags" placeholder="天猫, 抖音, 小红书" />
            </div>
            <div>
              <FieldLabel>主题标签</FieldLabel>
              <PixelInput name="topic_tags" placeholder="ROI优化, 创意, 数据分析" />
            </div>
          </div>

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full bg-[#1A202C] text-white py-2.5 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 pixel-border transition-colors"
          >
            {isSubmitting ? "提交中..." : "> 提交审核"}
          </button>
        </Form>
      </div>
    </div>
  );
}
