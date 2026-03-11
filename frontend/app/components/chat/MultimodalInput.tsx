import { useRef, useState, useCallback } from "react";

interface MultimodalInputProps {
  onSubmit: (data: { text?: string; files?: File[] }) => void;
  isLoading?: boolean;
  placeholder?: string;
}

export function MultimodalInput({
  onSubmit,
  isLoading = false,
  placeholder = "输入消息或粘贴内容... (Ctrl+Enter 发送)",
}: MultimodalInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [attachments, setAttachments] = useState<File[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handlePaste = async (e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;

    // Check if files were pasted
    const files = e.clipboardData?.files;
    if (files && files.length > 0) {
      e.preventDefault();
      const validFiles = Array.from(files).filter((f) => f.type.startsWith("image/"));
      if (validFiles.length > 0) {
        setAttachments((prev) => [...prev, ...validFiles]);
      }
      return;
    }

    // Check if text
    const textItem = Array.from(items).find((item) => item.type === "text/plain");
    if (textItem) {
      textItem.getAsString((text) => {
        if (text && text.trim() && textareaRef.current) {
          textareaRef.current.value = text;
        }
      });
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const ALLOWED_EXTS = [".txt", ".pdf", ".docx", ".pptx", ".md", ".xlsx", ".xls", ".csv",
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".mp3", ".wav", ".m4a", ".ogg", ".flac"];
  const isAllowedFile = (f: File) =>
    ALLOWED_EXTS.some((ext) => f.name.toLowerCase().endsWith(ext)) ||
    f.type.startsWith("image/");

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) {
      const validFiles = Array.from(files).filter(isAllowedFile);
      setAttachments((prev) => [...prev, ...validFiles]);
    }
  };

  const handleDragLeave = () => {
    setIsDragOver(false);
  };

  const removeAttachment = (idx: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleFileInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      setAttachments((prev) => [...prev, ...Array.from(files)]);
    }
    // reset so same file can be re-selected
    e.target.value = "";
  }, []);

  const handleSubmit = () => {
    const text = textareaRef.current?.value.trim();
    if (!text && attachments.length === 0) return;

    onSubmit({ text, files: attachments.length > 0 ? attachments : undefined });
    setAttachments([]);
    if (textareaRef.current) {
      textareaRef.current.value = "";
    }
  };

  return (
    <div
      className={`border-2 border-[#1A202C] bg-[#F8FAFC] transition-colors relative ${
        isDragOver ? "border-[#00D1FF] bg-[#CCF2FF]" : ""
      }`}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
    >
      {/* Attachment previews */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 p-2 border-b border-gray-200">
          {attachments.map((att, idx) => (
            <div
              key={idx}
              className="relative flex items-center gap-1.5 border-2 border-[#1A202C] bg-white px-2 py-1"
            >
              <span className="text-[9px] font-bold text-gray-600 max-w-[100px] truncate">
                {att.name}
              </span>
              <button
                onClick={() => removeAttachment(idx)}
                className="text-gray-400 hover:text-red-500 text-xs font-bold"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Input area */}
      <div className="flex items-end gap-2 p-2">
        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".txt,.pdf,.docx,.pptx,.md,.xlsx,.xls,.csv,.jpg,.jpeg,.png,.webp,.bmp,.mp3,.wav,.m4a,.ogg,.flac"
          className="hidden"
          onChange={handleFileInputChange}
        />
        {/* File upload button */}
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={isLoading}
          title="上传文件（支持 PDF/Word/PPT/Excel/TXT/MD/CSV/图片/音频）"
          className="flex-shrink-0 border-2 border-[#1A202C] bg-white text-[#1A202C] px-2 py-2 text-[10px] font-bold hover:bg-gray-100 disabled:opacity-50 transition-colors"
        >
          📎
        </button>
        <div className="flex-1 relative">
          <textarea
            ref={textareaRef}
            rows={2}
            disabled={isLoading}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={placeholder}
            className="w-full resize-none bg-transparent px-3 py-2 text-[11px] font-bold text-[#1A202C] placeholder:text-gray-400 placeholder:font-normal focus:outline-none leading-relaxed"
          />
        </div>
        <button
          onClick={handleSubmit}
          disabled={isLoading}
          className="bg-[#1A202C] text-white px-4 py-2 text-[10px] font-bold uppercase tracking-widest hover:bg-black disabled:opacity-50 transition-colors flex items-center gap-2 border-2 border-[#1A202C]"
        >
          {isLoading ? (
            <span className="flex gap-1">
              <div className="w-1 h-1 bg-[#00D1FF] animate-bounce [animation-delay:-0.3s]" />
              <div className="w-1 h-1 bg-[#00D1FF] animate-bounce [animation-delay:-0.15s]" />
              <div className="w-1 h-1 bg-[#00D1FF] animate-bounce" />
            </span>
          ) : (
            <>
              发送
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M13 7l5 5m0 0l-5 5m5-5H6" />
              </svg>
            </>
          )}
        </button>
      </div>

      {/* Drop zone hint */}
      {isDragOver && (
        <div className="absolute inset-0 border-2 border-[#00D1FF] bg-[#CCF2FF]/80 pointer-events-none flex items-center justify-center">
          <span className="text-[10px] font-bold uppercase tracking-widest text-[#1A202C]">
            松开以添加文件
          </span>
        </div>
      )}
    </div>
  );
}
