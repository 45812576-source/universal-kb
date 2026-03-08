import { useLoaderData, useNavigate } from "react-router";
import { requireUser } from "~/lib/auth.server";
import { getPendingConfirmations, confirmDraftFields, type DraftData } from "~/lib/draft-api";
import type { Route } from "./+types/index";

interface ConfirmationItem {
  draft_id: number;
  field: string;
  question: string;
  options?: string[];
  type?: "single_choice" | "text";
}

export async function loader({ request }: Route.LoaderArgs) {
  const { token } = await requireUser(request);
  const confirmations = await getPendingConfirmations(token);
  return { confirmations, token };
}

function ConfirmationCard({
  item,
  token,
  onAnswered,
}: {
  item: ConfirmationItem;
  token: string;
  onAnswered: () => void;
}) {
  const [customValue, setCustomValue] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleAnswer = async (value: string) => {
    setIsSubmitting(true);
    try {
      await confirmDraftFields(
        item.draft_id,
        { confirmed_fields: { [item.field]: value } },
        token
      );
      onAnswered();
    } catch (error) {
      console.error("Failed to submit answer:", error);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="border-2 border-[#1A202C] bg-white shadow-[4px_4px_0px_1px_rgba(0,0,0,0.3)] overflow-hidden">
      <div className="px-4 py-3 bg-yellow-50 border-b-2 border-[#1A202C]">
        <div className="flex items-center gap-2">
          <span className="text-[9px] font-bold uppercase tracking-widest text-yellow-600">
            ⚠ 待确认
          </span>
          <span className="text-[8px] text-gray-400 font-mono">
            Draft #{item.draft_id}
          </span>
        </div>
      </div>
      <div className="p-4">
        <p className="text-[11px] font-bold text-[#1A202C] mb-3">{item.question}</p>
        {item.options && item.options.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {item.options.map((opt) => (
              <button
                key={opt}
                onClick={() => handleAnswer(opt)}
                disabled={isSubmitting}
                className="px-4 py-2 text-[10px] font-bold uppercase tracking-widest border-2 border-[#1A202C] bg-white hover:bg-[#CCF2FF] hover:border-[#00D1FF] transition-colors disabled:opacity-50"
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
              className="flex-1 border-2 border-[#1A202C] px-3 py-2 text-[11px] focus:outline-none focus:border-[#00D1FF]"
            />
            <button
              onClick={() => customValue && handleAnswer(customValue)}
              disabled={!customValue || isSubmitting}
              className="px-4 py-2 text-[10px] font-bold uppercase tracking-widest bg-[#1A202C] text-white hover:bg-black disabled:opacity-50 transition-colors"
            >
              确认
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default function ConfirmationsPage() {
  const { confirmations, token } = useLoaderData<typeof loader>();
  const navigate = useNavigate();

  const handleAnswered = () => {
    // Refresh the list
    window.location.reload();
  };

  return (
    <div className="flex flex-col h-full bg-[#F0F4F8]">
      {/* Header */}
      <div className="border-b-2 border-[#1A202C] bg-white px-6 py-4 flex-shrink-0">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-sm font-bold uppercase tracking-widest text-[#1A202C]">
              待确认事项
            </h1>
            <p className="text-[10px] text-gray-500 mt-1">
              {confirmations.length} 个待确认的问题
            </p>
          </div>
          <button
            onClick={() => navigate("/app/chat")}
            className="px-4 py-2 text-[10px] font-bold uppercase tracking-widest border-2 border-[#1A202C] bg-white hover:bg-gray-100 transition-colors"
          >
            ← 返回聊天
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6">
        {confirmations.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-center">
            <div className="w-12 h-12 bg-green-50 border-2 border-green-400 flex items-center justify-center text-xl mb-4">
              ✓
            </div>
            <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400 mb-2">
              没有待确认事项
            </p>
            <p className="text-[9px] text-gray-400 max-w-xs">
              所有草稿都已确认或无需确认，继续在聊天中输入内容来生成新草稿
            </p>
          </div>
        ) : (
          <div className="max-w-xl mx-auto space-y-4">
            {confirmations.map((item, idx) => (
              <ConfirmationCard
                key={`${item.draft_id}-${item.field}`}
                item={item}
                token={token}
                onAnswered={handleAnswered}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
