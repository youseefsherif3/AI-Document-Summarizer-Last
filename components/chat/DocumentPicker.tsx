"use client";

import { Check, Files } from "lucide-react";

export interface PickableDocument {
  id: number;
  name: string;
}

interface DocumentPickerProps {
  documents: PickableDocument[];
  // Empty = all documents.
  selectedIds: number[];
  onChange: (ids: number[]) => void;
  disabled?: boolean;
}

export default function DocumentPicker({
  documents,
  selectedIds,
  onChange,
  disabled = false,
}: DocumentPickerProps) {
  if (documents.length === 0) return null;

  const allSelected = selectedIds.length === 0;
  const chipClass = (active: boolean) =>
    `flex max-w-56 items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition disabled:cursor-not-allowed disabled:opacity-50 ${
      active
        ? "border-violet-500/50 bg-violet-500/15 text-violet-200"
        : "border-white/10 bg-[#111827] text-gray-400 hover:border-violet-500/30 hover:text-white"
    }`;

  const toggle = (id: number) =>
    onChange(
      selectedIds.includes(id)
        ? selectedIds.filter((selected) => selected !== id)
        : [...selectedIds, id],
    );

  return (
    <div className="mt-3 flex shrink-0 items-start gap-2">
      <span className="mt-1 flex shrink-0 items-center gap-1.5 text-xs text-gray-500">
        <Files size={14} />
        Search in:
      </span>

      <div className="flex max-h-20 flex-wrap gap-2 overflow-y-auto">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onChange([])}
          aria-pressed={allSelected}
          className={chipClass(allSelected)}
        >
          {allSelected && <Check size={12} className="shrink-0" />}
          All documents
        </button>

        {documents.map((document) => {
          const active = selectedIds.includes(document.id);
          return (
            <button
              key={document.id}
              type="button"
              disabled={disabled}
              onClick={() => toggle(document.id)}
              aria-pressed={active}
              title={document.name}
              className={chipClass(active)}
            >
              {active && <Check size={12} className="shrink-0" />}
              <span className="truncate">{document.name}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
