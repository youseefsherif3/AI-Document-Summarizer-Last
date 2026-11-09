import {
  FileText,
  File as FileIcon,
  FileType,
  Trash2,
  Eye,
  LoaderCircle,
  X,
} from "lucide-react";

import type { UploadedDocument } from "@/types/document";
import FileStatus from "./FileStatus";

interface FileCardProps {
  document: UploadedDocument;
  onRemove: (id: string) => void;
  onViewSummary?: (id: string) => void;
  onCancel?: (id: string) => void;
}

const fileIcons = {
  pdf: FileText,
  docx: FileIcon,
  txt: FileType,
};

function formatFileSize(size: number) {
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }

  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

export default function FileCard({
  document,
  onRemove,
  onViewSummary,
  onCancel,
}: FileCardProps) {
  const Icon = fileIcons[document.type];

  const isProcessing =
    document.status === "extracting" || document.status === "summarizing";

  return (
    <div className="min-w-0 rounded-2xl border border-white/10 bg-[#111827] p-4 transition hover:border-violet-500/30 sm:p-5">
      {/* File Information */}
      <div className="flex items-start justify-between gap-3 sm:gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-violet-500/10">
            <Icon size={23} className="text-violet-400" />
          </div>

          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold text-white">
              {document.name}
            </h3>

            <p className="mt-1 text-xs text-gray-500">
              {document.type.toUpperCase()} · {formatFileSize(document.size)}
            </p>
          </div>
        </div>

        {isProcessing ? (
          <button
            type="button"
            onClick={() => onCancel?.(document.id)}
            aria-label={`Cancel ${document.name}`}
            className="shrink-0 rounded-lg p-2 text-gray-500 transition hover:bg-red-500/10 hover:text-red-400"
          >
            <X size={17} />
          </button>
        ) : (
          <button
            type="button"
            onClick={() => onRemove(document.id)}
            aria-label={`Remove ${document.name}`}
            className="shrink-0 rounded-lg p-2 text-gray-500 transition hover:bg-red-500/10 hover:text-red-400"
          >
            <Trash2 size={17} />
          </button>
        )}
      </div>

      {/* Status */}
      <div className="mt-5 flex items-center justify-between">
        <FileStatus status={document.status} />

        {isProcessing && (
          <span className="text-xs text-gray-400">{document.progress}%</span>
        )}
      </div>

      {/* Progress Bar */}
      {isProcessing && (
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/5">
          <div
            className="h-full rounded-full bg-violet-500 transition-all duration-300"
            style={{ width: `${document.progress}%` }}
          />
        </div>
      )}

      {/* Error */}
      {document.status === "failed" && document.error && (
        <p className="mt-3 text-xs text-red-400 [overflow-wrap:anywhere]">{document.error}</p>
      )}

      {/* View Summary */}
      {document.status === "completed" && document.summary && (
        <button
          type="button"
          onClick={() => onViewSummary?.(document.id)}
          className="mt-5 flex w-full items-center justify-center gap-2 rounded-xl border border-violet-500/20 bg-violet-500/5 py-2.5 text-sm font-medium text-violet-400 transition hover:bg-violet-500/10"
        >
          <Eye size={17} />
          View Summary
        </button>
      )}
    </div>
  );
}
