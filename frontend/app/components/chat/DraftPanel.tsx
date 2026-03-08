import type { DraftData } from "~/lib/draft-api";
import { DraftCard } from "./DraftCard";

interface DraftPanelProps {
  draft: DraftData | null;
  onConfirmFields: (confirmed: Record<string, any>, corrections?: Record<string, any>) => void;
  onConvert: () => void;
  onDiscard: () => void;
  isConverting?: boolean;
}

export function DraftPanel({
  draft,
  onConfirmFields,
  onConvert,
  onDiscard,
  isConverting = false,
}: DraftPanelProps) {
  if (!draft) {
    return (
      <div className="h-full flex items-center justify-center p-8 bg-gray-50 text-gray-500">
        <div className="text-center">
          <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400 mb-2">
            暂无草稿
          </p>
          <p className="text-[9px] text-gray-400">
            发送消息后，AI 会为您生成草稿
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-3">
      <DraftCard
        draft={draft}
        onConfirmFields={onConfirmFields}
        onConvert={onConvert}
        onDiscard={onDiscard}
        isConverting={isConverting}
      />
    </div>
  );
}
