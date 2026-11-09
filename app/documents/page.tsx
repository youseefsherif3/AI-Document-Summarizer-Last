
"use client";

import { useState } from "react";
import { AlertCircle, CheckCircle2, X } from "lucide-react";

import MainLayout from "@/components/layout/MainLayout";
import FileList from "@/components/documents/FileList";
import UploadZone from "../../components/documents/UploadZone";
import { useDocumentsContext } from "@/context/DocumentsContext";

export default function DocumentsPage() {
  const { documents, error, addFiles, removeDocument, cancelUpload, remainingSlots } =
    useDocumentsContext();

  const [selectedSummaryId, setSelectedSummaryId] = useState<string | null>(
    null,
  );

  const selectedDocument = documents.find(
    (document) => document.id === selectedSummaryId,
  );

  const errorMessages = error ? error.split("\n").filter(Boolean) : [];

  return (
    <MainLayout>
      <div className="mx-auto max-w-6xl space-y-6 sm:space-y-8">
        {/* Page Header */}
        <section>
          <h1 className="text-2xl font-bold text-white">My Documents</h1>
          <p className="mt-2 text-sm text-gray-400">
            Upload, manage, and view your document summaries.
          </p>
        </section>

        {/* Upload Section */}
        <UploadZone
          onFilesSelected={addFiles}
          remainingSlots={remainingSlots}
        />

        {/* Validation Messages */}
        {errorMessages.length > 0 && (
          <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-4">
            <div className="mb-3 flex items-center gap-2">
              <AlertCircle size={18} className="text-red-400" />
              <h3 className="text-sm font-semibold text-red-400">
                Upload Issues
              </h3>
            </div>

            <ul className="space-y-2">
              {errorMessages.map((message, index) => (
                <li
                  key={index}
                  className="flex items-start gap-2 text-sm text-gray-300"
                >
                  <X size={15} className="mt-0.5 shrink-0 text-red-400" />
                  <span className="min-w-0 [overflow-wrap:anywhere]">{message}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Upload Feedback */}
        {documents.length > 0 && (
          <div className="flex items-center gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-3">
            <CheckCircle2 size={17} className="text-emerald-400" />
            <p className="text-sm text-emerald-400">
              {documents.length} document(s) currently in your list.
            </p>
          </div>
        )}

        {/* Documents List */}
        <FileList
          documents={documents}
          onRemove={removeDocument}
          onViewSummary={setSelectedSummaryId}
          onCancel={cancelUpload}
        />

        {/* Summary Preview */}
        {selectedDocument?.summary && (
          <section className="rounded-2xl border border-white/10 bg-[#111827] p-4 sm:p-6">
            <div className="mb-5 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-lg font-semibold text-white">
                  Document Summary
                </h2>
                <p className="mt-1 text-sm text-gray-500 [overflow-wrap:anywhere]">
                  {selectedDocument.name}
                </p>
              </div>

              <button
                type="button"
                onClick={() => setSelectedSummaryId(null)}
                className="shrink-0 rounded-lg px-3 py-2 text-sm text-gray-400 hover:bg-white/5 hover:text-white"
              >
                Close
              </button>
            </div>

            <p className="whitespace-pre-wrap text-sm leading-7 text-gray-400 [overflow-wrap:anywhere]">
              {selectedDocument.summary}
            </p>
          </section>
        )}
      </div>
    </MainLayout>
  );
}
