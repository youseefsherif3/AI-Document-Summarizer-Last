import { FileText } from "lucide-react";

import type { UploadedDocument } from "@/types/document";
import FileCard from "./FileCard";
import EmptyState from "@/components/ui/EmptyState";

interface FileListProps {
  documents: UploadedDocument[];
  onRemove: (id: string) => void;
  onViewSummary: (id: string) => void;
  onCancel: (id: string) => void;
}

export default function FileList({
  documents,
  onRemove,
  onViewSummary,
  onCancel,
}: FileListProps) {
  return (
    <section>
      <div className="mb-5 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-white">Your Documents</h2>
          <p className="mt-1 text-sm text-gray-500">
            Manage and track your uploaded files
          </p>
        </div>

        <span className="shrink-0 rounded-full bg-violet-500/10 px-3 py-1.5 text-xs font-medium text-violet-400">
          {documents.length} Files
        </span>
      </div>

      {documents.length === 0 ? (
        <EmptyState
          icon={FileText}
          title="No documents yet"
          description="Upload your first document to get started."
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {documents.map((document) => (
            <FileCard
              key={document.id}
              document={document}
              onRemove={onRemove}
              onViewSummary={onViewSummary}
              onCancel={onCancel}
            />
          ))}
        </div>
      )}
    </section>
  );
}
