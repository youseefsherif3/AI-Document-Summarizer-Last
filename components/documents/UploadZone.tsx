"use client";

import { useRef, useState } from "react";
import { UploadCloud } from "lucide-react";
import { ALLOWED_EXTENSIONS, MAX_FILES_PER_SESSION } from "@/lib/constants";

interface UploadZoneProps {
  onFilesSelected: (files: File[]) => void;
  remainingSlots?: number;
}

export default function UploadZone({
  onFilesSelected,
  remainingSlots = MAX_FILES_PER_SESSION,
}: UploadZoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [isDragging, setIsDragging] = useState(false);

  const handleFiles = (files: FileList | null) => {
    if (!files || remainingSlots <= 0) return;

    onFilesSelected(Array.from(files));

    if (inputRef.current) {
      inputRef.current.value = "";
    }
  };

  return (
    <section className="rounded-2xl border border-white/10 bg-[#111827] p-6">
      <div className="mb-5">
        <h2 className="text-lg font-semibold text-white">Upload Documents</h2>
        <p className="mt-1 text-sm text-gray-500">
          Upload your files and let AI summarize them.
        </p>
      </div>

      <div
        onDragOver={(event) => {
          event.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setIsDragging(false);
          handleFiles(event.dataTransfer.files);
        }}
        className={`flex min-h-64 flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition ${
          isDragging
            ? "border-violet-500 bg-violet-500/10"
            : "border-white/15 bg-[#0B1120] hover:border-violet-500/50"
        }`}
      >
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-violet-500/10">
          <UploadCloud size={28} className="text-violet-400" />
        </div>

        <h3 className="text-base font-semibold text-white">
          Drag and drop your files here
        </h3>

        <p className="mt-2 text-sm text-gray-500">
          or click below to browse your device
        </p>

        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={remainingSlots <= 0}
          className="mt-5 rounded-xl bg-violet-600 px-6 py-2.5 text-sm font-medium text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Browse Files
        </button>

        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ALLOWED_EXTENSIONS.join(",")}
          className="hidden"
          onChange={(event) => handleFiles(event.target.files)}
        />

        <p className="mt-5 text-xs text-gray-500">
          PDF, DOCX, TXT · Max 10 MB per file
        </p>
      </div>

      <div className="mt-4 flex items-center justify-between text-xs text-gray-500">
        <span>Maximum 3 files per session</span>
        <span>{remainingSlots} slots remaining</span>
      </div>
    </section>
  );
}
