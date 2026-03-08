import { useState } from "react";
import type { DraftData, PendingQuestion } from "~/lib/draft-api";

const OBJECT_TYPE_LABELS: Record<string, string> = {
  knowledge: "📝 知识草稿",
  opportunity: "🏢 商机草稿",
  feedback: "💬 反馈草稿",
  unknown: "❓ 未识别",
};

const OBJECT_TYPE_COLORS: Record<string, string> = {
  knowledge: "#00D1FF",
  opportunity: "#00CC99",
  feedback: "#F6AD55",
  unknown: "#A0AEC0",
};

interface DraftCardProps {
  draft: DraftData;
  onConfirmFields: (confirmed: Record<string, any>, corrections?: Record<string, any>) => void;
  onConvert: () => void;
  onDiscard: () => void;
  isConverting?: boolean;
}

// Also export to DraftPanel
export type { DraftCardProps };

function FieldRow({ label, value }: { label: string; value: any }) {
  const displayValue = Array.isArray(value)
    ? value.join(", ")
    : typeof value === "boolean"
    ? value ? "是" : "否"
    : value?.toString?.() || "—";
  return (
    <div className="flex gap-2 py-1">
      <span className="text-[9px] font-bold text-gray-400 uppercase w-24 flex-shrink-0">{label}</span>
      <span className="text-[11px] text-gray-700">{displayValue}</span>
    </div>
  );
}

function PendingQuestionItem({
  question,
  onAnswer,
}: {
  question: PendingQuestion;
  onAnswer: (field: string, value: string) => void;
}) {
  const [customValue, setCustomValue] = useState("");

  return (
    <div className="border-l-2 border-yellow-400 pl-3 py-2">
      <p className="text-[10px] font-bold text-yellow-600 mb-1.5">{question.question}</p>
      {question.type === "single_choice" && question.options ? (
        <div className="flex flex-wrap gap-1.5">
          {question.options.map((opt) => (
            <button
              key={opt}
              onClick={() => onAnswer(question.field, opt)}
              className="px-3 py-1 text-[9px] font-bold uppercase border-2 border-[#1A202C] bg-white hover:bg-[#CCF2FF] hover:border-[#00D1FF] transition-colors"
            >
              {opt}
            </button>
          ))}
        </div>
      ) : (
        <div className="flex gap-2">
          <input
            value={customValue}
            onChange={(e) => setCustomValue(e.target.value)}
            placeholder="输入答案..."
            className="flex-1 border-2 border-[#1A202C] px-2 py-1 text-[10px] focus:outline-none focus:border-[#00D1FF]"
          />
          <button
            onClick={() => customValue && onAnswer(question.field, customValue)}
            disabled={!customValue}
            className="px-3 py-1 text-[9px] font-bold bg-[#1A202C] text-white disabled:opacity-50"
          >
            确认
          </button>
        </div>
      )}
    </div>
  );
}

export function DraftCard({
  draft,
  onConfirmFields,
  onConvert,
  onDiscard,
  isConverting = false,
}: DraftCardProps) {
  const color = OBJECT_TYPE_COLORS[draft.object_type] || "#A0AEC0";
  const label = OBJECT_TYPE_LABELS[draft.object_type] || "草稿";

  const isConverted = draft.status === "converted";
  const isDiscarded = draft.status === "discarded";

  // Render fields based on object type
  const renderFields = () => {
    const f = draft.fields;
    if (draft.object_type === "knowledge" && f) {
      return (
        <>
          {f.knowledge_type && <FieldRow label="类型" value={f.knowledge_type} />}
          {f.industry_tags?.length > 0 && <FieldRow label="行业" value={f.industry_tags} />}
          {f.platform_tags?.length > 0 && <FieldRow label="平台" value={f.platform_tags} />}
          {f.topic_tags?.length > 0 && <FieldRow label="主题" value={f.topic_tags} />}
          {f.visibility && <FieldRow label="可见" value={f.visibility === "all" ? "全员" : "本部门"} />}
        </>
      );
    }
    if (draft.object_type === "opportunity" && f) {
      return (
        <>
          {f.customer_name && <FieldRow label="客户" value={f.customer_name} />}
          {f.industry && <FieldRow label="行业" value={f.industry} />}
          {f.stage && <FieldRow label="阶段" value={f.stage} />}
          {f.priority && <FieldRow label="优先级" value={f.priority} />}
        </>
      );
    }
    if (draft.object_type === "feedback" && f) {
      return (
        <>
          {f.customer_name && <FieldRow label="客户" value={f.customer_name} />}
          {f.feedback_type && <FieldRow label="类型" value={f.feedback_type} />}
          {f.severity && <FieldRow label="严重度" value={f.severity} />}
          {f.routed_team && <FieldRow label="建议流转" value={f.routed_team} />}
        </>
      );
    }
    return null;
  };

  const handleAnswer = (field: string, value: string) => {
    onConfirmFields({ [field]: value });
  };

  return (
    <div className="border-2 border-[#1A202C] bg-white shadow-[4px_4px_0px_1px_rgba(0,0,0,0.3)] overflow-hidden">
      {/* Header */}
      <div className="px-4 py-2.5 flex items-center justify-between" style={{ backgroundColor: `${color}18` }}>
        <span className="text-[10px] font-bold uppercase tracking-widest">{label}</span>
        <span className="text-[9px] font-bold uppercase text-gray-600">
          {draft.title?.slice(0, 30) || "(无标题)"}
        </span>
      </div>

      {/* Summary */}
      {draft.summary && (
        <div className="px-4 py-2 bg-gray-50 border-b border-gray-100">
          <p className="text-[11px] text-gray-600 leading-relaxed">{draft.summary}</p>
        </div>
      )}

      {/* Fields */}
      <div className="p-4 space-y-0">{renderFields()}</div>

      {/* Pending questions */}
      {draft.pending_questions.length > 0 && !isConverted && !isDiscarded && (
        <div className="px-4 space-y-2">
          <p className="text-[9px] font-bold uppercase tracking-widest text-yellow-600">
            ⚠ 待确认
          </p>
          {draft.pending_questions.map((q) => (
            <PendingQuestionItem key={q.field} question={q} onAnswer={handleAnswer} />
          ))}
        </div>
      )}

      {/* Actions */}
      {!isConverted && !isDiscarded && (
        <div className="flex flex-wrap gap-2 p-4 border-t border-gray-100">
            <button
              onClick={onConvert}
              disabled={isConverting}
              className="px-4 py-1.5 text-[9px] font-bold uppercase tracking-widest bg-[#1A202C] text-white hover:bg-black disabled:opacity-50 transition-colors"
            >
              {isConverting ? "保存中..." : draft.object_type === "knowledge" ? "保存知识" : draft.object_type === "opportunity" ? "保存商机" : "保存反馈"}
            </button>
            {draft.suggested_actions
              .filter((a) => !a.includes("保存"))
              .slice(0, 2)
              .map((action) => (
                <button
                  key={action}
                  className="px-4 py-1.5 text-[9px] font-bold uppercase tracking-widest border-2 border-[#1A202C] bg-white hover:bg-gray-100 transition-colors"
                >
                  {action}
                </button>
              ))}
            <button
              onClick={onDiscard}
              className="px-3 py-1.5 text-[9px] font-bold uppercase tracking-widest text-gray-400 hover:text-red-500 transition-colors ml-auto"
            >
              丢弃
            </button>
          </div>
      )}

      {/* Status badges */}
      {isConverted && (
        <div className="px-4 py-2 bg-green-50 border-t border-green-400">
          <span className="text-[9px] font-bold uppercase text-green-600">✓ 已保存</span>
        </div>
      )}
      {isDiscarded && (
        <div className="px-4 py-2 bg-gray-50 border-t border-gray-400">
          <span className="text-[9px] font-bold uppercase text-gray-400">已丢弃</span>
        </div>
      )}
    </div>
  );
}
