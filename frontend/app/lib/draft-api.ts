// frontend/app/lib/draft-api.ts
import { apiFetch } from "~/lib/api";

export interface PendingQuestion {
  field: string;
  question: string;
  options?: string[];
  type: "single_choice" | "text";
}

export interface DraftData {
  id: number;
  object_type: "knowledge" | "opportunity" | "feedback" | "unknown";
  title: string | null;
  summary: string | null;
  fields: Record<string, any>;
  tags: { industry?: string[]; platform?: string[]; topic?: string[] };
  pending_questions: PendingQuestion[];
  confirmed_fields: Record<string, any>;
  suggested_actions: string[];
  status: "draft" | "waiting_confirmation" | "confirmed" | "discarded" | "converted";
  formal_object_id: number | null;
}

export async function submitRawInput(
  params: {
    text?: string;
    files?: File[];
    conversationId?: number;
    workspaceId?: number;
  },
  token: string
): Promise<{ raw_input_id: number; draft: DraftData }> {
  const form = new FormData();
  if (params.text) form.append("text", params.text);
  if (params.conversationId) form.append("conversation_id", String(params.conversationId));
  if (params.workspaceId) form.append("workspace_id", String(params.workspaceId));
  form.append("source_type", params.files?.length ? "file" : "text");
  params.files?.forEach((f) => form.append("files", f));

  return apiFetch("/api/raw-inputs", { method: "POST", body: form, token });
}

export async function confirmDraftFields(
  draftId: number,
  params: { confirmed_fields?: Record<string, any>; corrections?: Record<string, any> },
  token: string
): Promise<DraftData> {
  return apiFetch(`/api/drafts/${draftId}/confirm`, {
    method: "PATCH",
    body: JSON.stringify(params),
    token,
  });
}

export async function convertDraft(draftId: number, token: string): Promise<DraftData> {
  return apiFetch(`/api/drafts/${draftId}/convert`, { method: "POST", token });
}

export async function discardDraft(draftId: number, token: string): Promise<DraftData> {
  return apiFetch(`/api/drafts/${draftId}/discard`, { method: "POST", token });
}

export async function getDraft(draftId: number, token: string): Promise<DraftData> {
  return apiFetch(`/api/drafts/${draftId}`, { token });
}

export async function listDrafts(token: string): Promise<DraftData[]> {
  return apiFetch("/api/drafts", { token });
}

export async function getPendingConfirmations(token: string): Promise<{
  draft_id: number;
  field: string;
  question: string;
  options?: string[];
}[]> {
  return apiFetch("/api/confirmations", { token });
}
