import { useState } from "react";
import { useLoaderData } from "react-router";
import type { Route } from "./+types/index";
import { requireUser } from "~/lib/auth.server";
import { apiFetch } from "~/lib/api";

interface Task {
  id: number;
  title: string;
  description: string | null;
  priority: "urgent_important" | "important" | "urgent" | "neither";
  status: "pending" | "in_progress" | "done" | "cancelled";
  due_date: string | null;
  assignee_id: number;
  assignee_name: string | null;
  created_by_id: number;
  creator_name: string | null;
  source_type: string;
  source_id: number | null;
  conversation_id: number | null;
  created_at: string;
}

interface TaskStats {
  urgent_important: number;
  important: number;
  urgent: number;
  neither: number;
  overdue: number;
  total_pending: number;
}

interface User {
  id: number;
  display_name: string;
}

const PRIORITY_LABELS: Record<string, string> = {
  urgent_important: "重要且紧急",
  important: "重要不紧急",
  urgent: "紧急不重要",
  neither: "不重要不紧急",
};

const PRIORITY_COLORS: Record<string, { border: string; bg: string; badge: string; text: string }> = {
  urgent_important: { border: "border-red-400", bg: "bg-red-50", badge: "bg-red-100 text-red-700", text: "text-red-600" },
  important: { border: "border-yellow-400", bg: "bg-yellow-50", badge: "bg-yellow-100 text-yellow-700", text: "text-yellow-600" },
  urgent: { border: "border-orange-400", bg: "bg-orange-50", badge: "bg-orange-100 text-orange-700", text: "text-orange-600" },
  neither: { border: "border-gray-300", bg: "bg-white", badge: "bg-gray-100 text-gray-500", text: "text-gray-500" },
};

const SOURCE_LABELS: Record<string, string> = {
  chat_message: "对话",
  draft: "草稿",
  manual: "手动",
  ai_generated: "AI生成",
};

export async function loader({ request }: Route.LoaderArgs) {
  const { token, user } = await requireUser(request);
  const [tasks, stats, users] = await Promise.all([
    apiFetch("/api/tasks", { token }),
    apiFetch("/api/tasks/stats", { token }),
    apiFetch("/api/tasks/users", { token }).catch(() => []),
  ]);
  return { tasks, stats, token, currentUser: user, users };
}

export default function TasksPage() {
  const { tasks: initialTasks, stats: initialStats, token, currentUser, users } = useLoaderData<typeof loader>() as {
    tasks: Task[];
    stats: TaskStats;
    token: string;
    currentUser: { id: number; display_name: string; role: string };
    users: User[];
  };

  const [tasks, setTasks] = useState<Task[]>(initialTasks);
  const [stats, setStats] = useState<TaskStats>(initialStats);
  const [showNewForm, setShowNewForm] = useState(false);
  const [showGenerateForm, setShowGenerateForm] = useState(false);
  const [generatedTasks, setGeneratedTasks] = useState<any[] | null>(null);
  const [filterPriority, setFilterPriority] = useState<string>("all");
  const [loading, setLoading] = useState(false);

  // New task form state
  const [newTitle, setNewTitle] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newPriority, setNewPriority] = useState<string>("neither");
  const [newDueDate, setNewDueDate] = useState("");
  const [newAssigneeId, setNewAssigneeId] = useState<string>(String(currentUser.id));

  // AI generate form state
  const [genDesc, setGenDesc] = useState("");
  const [genAssigneeId, setGenAssigneeId] = useState<string>(String(currentUser.id));

  async function refreshTasks() {
    const [t, s] = await Promise.all([
      apiFetch("/api/tasks", { token }),
      apiFetch("/api/tasks/stats", { token }),
    ]);
    setTasks(t);
    setStats(s);
  }

  async function handleCreateTask(e: React.FormEvent) {
    e.preventDefault();
    if (!newTitle.trim()) return;
    setLoading(true);
    try {
      await apiFetch("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          title: newTitle.trim(),
          description: newDesc.trim() || null,
          priority: newPriority,
          due_date: newDueDate ? new Date(newDueDate).toISOString() : null,
          assignee_id: Number(newAssigneeId),
        }),
        token,
      });
      setShowNewForm(false);
      setNewTitle(""); setNewDesc(""); setNewPriority("neither"); setNewDueDate("");
      setNewAssigneeId(String(currentUser.id));
      await refreshTasks();
    } finally {
      setLoading(false);
    }
  }

  async function handleStatusChange(task: Task, newStatus: string) {
    await apiFetch(`/api/tasks/${task.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: newStatus }),
      token,
    });
    await refreshTasks();
  }

  async function handleDelete(task: Task) {
    if (!confirm(`确认删除任务「${task.title}」？`)) return;
    await apiFetch(`/api/tasks/${task.id}`, { method: "DELETE", token });
    await refreshTasks();
  }

  async function handleGenerate(e: React.FormEvent) {
    e.preventDefault();
    if (!genDesc.trim()) return;
    setLoading(true);
    try {
      const result = await apiFetch("/api/tasks/generate", {
        method: "POST",
        body: JSON.stringify({ description: genDesc.trim(), assignee_id: Number(genAssigneeId) }),
        token,
      });
      setGeneratedTasks(result.tasks);
    } finally {
      setLoading(false);
    }
  }

  async function handleConfirmGenerated() {
    if (!generatedTasks) return;
    setLoading(true);
    try {
      for (const t of generatedTasks) {
        await apiFetch("/api/tasks", {
          method: "POST",
          body: JSON.stringify({
            title: t.title,
            description: t.description,
            priority: t.priority,
            assignee_id: Number(genAssigneeId),
            source_type: "ai_generated",
          }),
          token,
        });
      }
      setGeneratedTasks(null);
      setShowGenerateForm(false);
      setGenDesc("");
      await refreshTasks();
    } finally {
      setLoading(false);
    }
  }

  const filteredTasks = filterPriority === "all"
    ? tasks
    : tasks.filter((t) => t.priority === filterPriority);

  const now = new Date();
  const isOverdue = (t: Task) => t.due_date && new Date(t.due_date) < now;

  const QUADRANT_ICONS: Record<string, string> = {
    urgent_important: "🔴",
    important: "🟡",
    urgent: "🟠",
    neither: "⚪",
  };

  return (
    <div className="p-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-sm font-bold uppercase tracking-widest text-[#1A202C]">待办任务</h1>
          <p className="text-[10px] text-gray-500 mt-0.5 uppercase tracking-wide">
            {stats.total_pending} 个待处理 {stats.overdue > 0 && `· ${stats.overdue} 个已过期`}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => { setShowGenerateForm(true); setShowNewForm(false); }}
            className="text-[10px] font-bold uppercase px-3 py-1.5 border-2 border-[#00A3C4] text-[#00A3C4] hover:bg-[#CCF2FF] transition-colors"
          >
            ✨ AI 拆解
          </button>
          <button
            onClick={() => { setShowNewForm(true); setShowGenerateForm(false); }}
            className="text-[10px] font-bold uppercase px-3 py-1.5 border-2 border-[#1A202C] bg-[#1A202C] text-white hover:bg-black transition-colors"
          >
            ➕ 新建任务
          </button>
        </div>
      </div>

      {/* Eisenhower Matrix Stats */}
      <div className="grid grid-cols-4 gap-3 mb-6">
        {(["urgent_important", "important", "urgent", "neither"] as const).map((p) => {
          const c = PRIORITY_COLORS[p];
          return (
            <button
              key={p}
              onClick={() => setFilterPriority(filterPriority === p ? "all" : p)}
              className={`border-2 p-3 text-left transition-all ${c.border} ${c.bg} ${filterPriority === p ? "ring-2 ring-offset-1 ring-[#1A202C]" : "hover:opacity-80"}`}
            >
              <div className="text-lg mb-1">{QUADRANT_ICONS[p]}</div>
              <div className={`text-xl font-bold ${c.text}`}>{stats[p]}</div>
              <div className="text-[9px] font-bold uppercase tracking-wide text-gray-500 mt-0.5">
                {PRIORITY_LABELS[p]}
              </div>
            </button>
          );
        })}
      </div>

      {/* New Task Form */}
      {showNewForm && (
        <div className="border-2 border-[#1A202C] bg-white p-4 mb-4">
          <h2 className="text-[10px] font-bold uppercase tracking-widest mb-3">新建任务</h2>
          <form onSubmit={handleCreateTask} className="space-y-2">
            <input
              type="text"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="任务标题"
              required
              className="w-full border border-gray-300 px-2 py-1.5 text-xs font-bold text-[#1A202C] focus:outline-none focus:border-[#00A3C4]"
            />
            <textarea
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
              placeholder="任务描述（可选）"
              rows={2}
              className="w-full border border-gray-300 px-2 py-1.5 text-xs text-[#1A202C] focus:outline-none focus:border-[#00A3C4] resize-none"
            />
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="block text-[9px] font-bold uppercase text-gray-500 mb-1">优先级</label>
                <select
                  value={newPriority}
                  onChange={(e) => setNewPriority(e.target.value)}
                  className="w-full border border-gray-300 px-2 py-1.5 text-xs font-bold focus:outline-none focus:border-[#00A3C4]"
                >
                  <option value="urgent_important">🔴 重要且紧急</option>
                  <option value="important">🟡 重要不紧急</option>
                  <option value="urgent">🟠 紧急不重要</option>
                  <option value="neither">⚪ 不重要不紧急</option>
                </select>
              </div>
              <div>
                <label className="block text-[9px] font-bold uppercase text-gray-500 mb-1">截止日期</label>
                <input
                  type="datetime-local"
                  value={newDueDate}
                  onChange={(e) => setNewDueDate(e.target.value)}
                  className="w-full border border-gray-300 px-2 py-1.5 text-xs focus:outline-none focus:border-[#00A3C4]"
                />
              </div>
              <div>
                <label className="block text-[9px] font-bold uppercase text-gray-500 mb-1">指派给</label>
                <select
                  value={newAssigneeId}
                  onChange={(e) => setNewAssigneeId(e.target.value)}
                  className="w-full border border-gray-300 px-2 py-1.5 text-xs focus:outline-none focus:border-[#00A3C4]"
                >
                  <option value={currentUser.id}>{currentUser.display_name}（我）</option>
                  {(users as User[]).filter((u) => u.id !== currentUser.id).map((u) => (
                    <option key={u.id} value={u.id}>{u.display_name}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="flex gap-2 pt-1">
              <button
                type="submit"
                disabled={loading}
                className="text-[10px] font-bold uppercase px-4 py-1.5 bg-[#1A202C] text-white hover:bg-black disabled:opacity-40 transition-colors"
              >
                {loading ? "创建中..." : "创建"}
              </button>
              <button
                type="button"
                onClick={() => setShowNewForm(false)}
                className="text-[10px] font-bold uppercase px-4 py-1.5 border border-gray-300 text-gray-500 hover:bg-gray-100 transition-colors"
              >
                取消
              </button>
            </div>
          </form>
        </div>
      )}

      {/* AI Generate Form */}
      {showGenerateForm && (
        <div className="border-2 border-[#00A3C4] bg-[#EBF4F7] p-4 mb-4">
          <h2 className="text-[10px] font-bold uppercase tracking-widest mb-3 text-[#00A3C4]">✨ AI 任务拆解</h2>
          {!generatedTasks ? (
            <form onSubmit={handleGenerate} className="space-y-2">
              <textarea
                value={genDesc}
                onChange={(e) => setGenDesc(e.target.value)}
                placeholder="描述要完成的工作，AI 将自动拆解为多个可执行任务..."
                rows={3}
                required
                className="w-full border border-[#00A3C4] px-2 py-1.5 text-xs text-[#1A202C] focus:outline-none bg-white resize-none"
              />
              <div className="flex items-center gap-4">
                <div>
                  <label className="block text-[9px] font-bold uppercase text-gray-500 mb-1">指派给</label>
                  <select
                    value={genAssigneeId}
                    onChange={(e) => setGenAssigneeId(e.target.value)}
                    className="border border-[#00A3C4] px-2 py-1.5 text-xs focus:outline-none bg-white"
                  >
                    <option value={currentUser.id}>{currentUser.display_name}（我）</option>
                    {(users as User[]).filter((u) => u.id !== currentUser.id).map((u) => (
                      <option key={u.id} value={u.id}>{u.display_name}</option>
                    ))}
                  </select>
                </div>
                <div className="flex gap-2 mt-4">
                  <button
                    type="submit"
                    disabled={loading}
                    className="text-[10px] font-bold uppercase px-4 py-1.5 bg-[#00A3C4] text-white hover:bg-[#007A96] disabled:opacity-40 transition-colors"
                  >
                    {loading ? "拆解中..." : "AI 拆解"}
                  </button>
                  <button
                    type="button"
                    onClick={() => { setShowGenerateForm(false); setGeneratedTasks(null); setGenDesc(""); }}
                    className="text-[10px] font-bold uppercase px-4 py-1.5 border border-gray-300 text-gray-500 hover:bg-gray-100 transition-colors"
                  >
                    取消
                  </button>
                </div>
              </div>
            </form>
          ) : (
            <div>
              <p className="text-[10px] font-bold uppercase text-[#00A3C4] mb-3">
                AI 已拆解为 {generatedTasks.length} 个任务，确认后批量创建：
              </p>
              <div className="space-y-2 mb-3">
                {generatedTasks.map((t, i) => {
                  const c = PRIORITY_COLORS[t.priority] || PRIORITY_COLORS.neither;
                  return (
                    <div key={i} className={`border ${c.border} p-2 bg-white`}>
                      <div className="flex items-start gap-2">
                        <span className={`text-[9px] font-bold px-1.5 py-0.5 ${c.badge} flex-shrink-0 mt-0.5`}>
                          {PRIORITY_LABELS[t.priority] || t.priority}
                        </span>
                        <div>
                          <p className="text-[11px] font-bold text-[#1A202C]">{t.title}</p>
                          {t.description && (
                            <p className="text-[10px] text-gray-500 mt-0.5">{t.description}</p>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={handleConfirmGenerated}
                  disabled={loading}
                  className="text-[10px] font-bold uppercase px-4 py-1.5 bg-[#00A3C4] text-white hover:bg-[#007A96] disabled:opacity-40 transition-colors"
                >
                  {loading ? "创建中..." : "确认创建"}
                </button>
                <button
                  onClick={() => setGeneratedTasks(null)}
                  className="text-[10px] font-bold uppercase px-4 py-1.5 border border-gray-300 text-gray-500 hover:bg-gray-100 transition-colors"
                >
                  重新生成
                </button>
                <button
                  onClick={() => { setShowGenerateForm(false); setGeneratedTasks(null); setGenDesc(""); }}
                  className="text-[10px] font-bold uppercase px-4 py-1.5 border border-gray-300 text-gray-500 hover:bg-gray-100 transition-colors"
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Filter tabs */}
      <div className="flex gap-1 mb-4">
        {[
          { key: "all", label: "全部" },
          { key: "urgent_important", label: "🔴 重要且紧急" },
          { key: "important", label: "🟡 重要不紧急" },
          { key: "urgent", label: "🟠 紧急不重要" },
          { key: "neither", label: "⚪ 其他" },
        ].map((f) => (
          <button
            key={f.key}
            onClick={() => setFilterPriority(f.key)}
            className={`text-[9px] font-bold uppercase px-2 py-1 border transition-colors ${
              filterPriority === f.key
                ? "border-[#1A202C] bg-[#1A202C] text-white"
                : "border-gray-300 text-gray-500 hover:border-gray-400"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Task List */}
      {filteredTasks.length === 0 ? (
        <div className="text-center py-16">
          <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">暂无待办任务</p>
        </div>
      ) : (
        <div className="space-y-2">
          {filteredTasks.map((task) => {
            const c = PRIORITY_COLORS[task.priority] || PRIORITY_COLORS.neither;
            const overdue = isOverdue(task);
            return (
              <div
                key={task.id}
                className={`border-2 p-3 transition-colors ${overdue ? "border-red-400 bg-red-50" : `${c.border} ${c.bg}`}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-2 flex-1 min-w-0">
                    {/* Status toggle */}
                    <button
                      onClick={() => handleStatusChange(task, task.status === "done" ? "pending" : "done")}
                      className={`flex-shrink-0 w-4 h-4 border-2 mt-0.5 transition-colors ${
                        task.status === "done"
                          ? "bg-[#00CC99] border-[#00CC99]"
                          : "border-gray-400 hover:border-[#00CC99]"
                      }`}
                      title={task.status === "done" ? "标记为未完成" : "标记为完成"}
                    />
                    <div className="min-w-0 flex-1">
                      <p className={`text-xs font-bold text-[#1A202C] ${task.status === "done" ? "line-through opacity-50" : ""}`}>
                        {task.title}
                      </p>
                      {task.description && (
                        <p className="text-[10px] text-gray-500 mt-0.5 line-clamp-2">{task.description}</p>
                      )}
                      <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                        <span className={`text-[9px] font-bold px-1.5 py-0.5 ${c.badge}`}>
                          {PRIORITY_LABELS[task.priority]}
                        </span>
                        {task.source_type && task.source_type !== "manual" && (
                          <span className="text-[9px] font-bold px-1.5 py-0.5 bg-gray-100 text-gray-500">
                            来自{SOURCE_LABELS[task.source_type] || task.source_type}
                          </span>
                        )}
                        {task.due_date && (
                          <span className={`text-[9px] font-bold ${overdue ? "text-red-600" : "text-gray-500"}`}>
                            {overdue ? "⚠ 已过期 " : "⏰ "}
                            {new Date(task.due_date).toLocaleDateString("zh-CN")}
                          </span>
                        )}
                        {task.assignee_name && task.assignee_id !== currentUser.id && (
                          <span className="text-[9px] text-gray-400">→ {task.assignee_name}</span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    {task.status !== "done" && (
                      <button
                        onClick={() => handleStatusChange(task, task.status === "in_progress" ? "pending" : "in_progress")}
                        className={`text-[9px] font-bold uppercase px-1.5 py-0.5 border transition-colors ${
                          task.status === "in_progress"
                            ? "border-blue-400 bg-blue-100 text-blue-700"
                            : "border-gray-300 text-gray-400 hover:border-blue-300"
                        }`}
                      >
                        {task.status === "in_progress" ? "进行中" : "开始"}
                      </button>
                    )}
                    {task.created_by_id === currentUser.id && (
                      <button
                        onClick={() => handleDelete(task)}
                        className="text-[9px] font-bold uppercase px-1.5 py-0.5 border border-gray-200 text-gray-300 hover:border-red-300 hover:text-red-400 transition-colors"
                      >
                        ✕
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
