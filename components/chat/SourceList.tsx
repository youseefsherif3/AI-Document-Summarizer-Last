"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp, FileText } from "lucide-react";

import type { ChatSource } from "@/types/chat";

interface SourceListProps {
  messageId: string;
  sources: ChatSource[];
  citedNumbers: number[];
  activeSource: number | null;
  onSelect: (n: number) => void;
  isStreaming: boolean;
}

export function sourceElementId(messageId: string, n: number) {
  return `source-${messageId}-${n}`;
}

export default function SourceList({
  messageId,
  sources,
  citedNumbers,
  activeSource,
  onSelect,
  isStreaming,
}: SourceListProps) {
  const [showAll, setShowAll] = useState(false);

  // Bring the source into view when its citation is clicked.
  useEffect(() => {
    if (activeSource == null) return;
    document
      .getElementById(sourceElementId(messageId, activeSource))
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activeSource, messageId]);

  const cited = citedNumbers
    .map((n) => sources.find((source) => source.n === n))
    .filter((source): source is ChatSource => source !== undefined);

  // Nothing to show yet: no sources, or the answer hasn't cited any so far.
  if (sources.length === 0 || (cited.length === 0 && isStreaming)) return null;

  // Cited sources first; the other sources that were searched are behind a toggle.
  const visible = showAll ? sources : cited;
  const hiddenCount = sources.length - cited.length;

  return (
    <div className="mt-4 border-t border-white/10 pt-3">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
        {cited.length > 0 ? "Sources" : "No sources cited"}
      </p>

      <ol className="space-y-2">
        {visible.map((source) => {
          const isActive = activeSource === source.n;
          return (
            <li key={source.n} id={sourceElementId(messageId, source.n)}>
              <button
                type="button"
                onClick={() => onSelect(source.n)}
                className={`w-full rounded-xl border p-2.5 text-left transition sm:p-3 ${
                  isActive
                    ? "border-violet-500/60 bg-violet-500/10"
                    : "border-white/10 bg-[#0B1120] hover:border-violet-500/30"
                }`}
              >
                <div className="flex min-w-0 items-center gap-2">
                  <span
                    className={`flex h-5 min-w-5 shrink-0 items-center justify-center rounded-md px-1.5 text-[11px] font-semibold ${
                      isActive ? "bg-violet-500 text-white" : "bg-violet-500/15 text-violet-300"
                    }`}
                  >
                    {source.n}
                  </span>
                  <FileText size={14} className="shrink-0 text-violet-400" />
                  <span className="min-w-0 truncate text-xs font-medium text-gray-200">
                    {source.filename}
                  </span>
                  <span className="shrink-0 text-xs text-gray-500">
                    {source.page != null ? `p. ${source.page}` : "no page"}
                  </span>
                </div>
                <p className="mt-1.5 line-clamp-3 text-xs leading-5 text-gray-400 [overflow-wrap:anywhere] sm:line-clamp-2">
                  {source.snippet}
                </p>
              </button>
            </li>
          );
        })}
      </ol>

      {hiddenCount > 0 && (
        <button
          type="button"
          onClick={() => setShowAll((current) => !current)}
          className="mt-2 flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-gray-400 transition hover:bg-white/5 hover:text-white"
        >
          {showAll ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          {showAll
            ? cited.length > 0
              ? "Show cited sources only"
              : "Hide sources searched"
            : `Show ${hiddenCount} ${cited.length > 0 ? "more " : ""}source${
                hiddenCount === 1 ? "" : "s"
              } searched`}
        </button>
      )}
    </div>
  );
}
