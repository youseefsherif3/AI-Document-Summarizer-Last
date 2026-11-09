"use client";

import type { ReactNode } from "react";

import type { ChatSource } from "@/types/chat";

const CITATION = /\[(\d+)\]/g;

/** Source numbers cited in the answer, in order of first appearance. */
export function citedSourceNumbers(text: string, sources: ChatSource[]): number[] {
  const known = new Set(sources.map((source) => source.n));
  const cited: number[] = [];
  for (const match of text.matchAll(CITATION)) {
    const n = Number(match[1]);
    if (known.has(n) && !cited.includes(n)) cited.push(n);
  }
  return cited;
}

interface AnswerTextProps {
  // The whole answer received so far. Citations are found in this combined text,
  // never in single stream events, because "[1]" can arrive as "[", "1", "]".
  text: string;
  sources: ChatSource[];
  activeSource: number | null;
  onCite: (n: number) => void;
}

export default function AnswerText({ text, sources, activeSource, onCite }: AnswerTextProps) {
  const byNumber = new Map(sources.map((source) => [source.n, source]));
  const parts: ReactNode[] = [];
  let last = 0;

  for (const match of text.matchAll(CITATION)) {
    const n = Number(match[1]);
    const source = byNumber.get(n);
    const index = match.index ?? 0;
    // A number with no matching source stays plain text.
    if (!source) continue;

    if (index > last) parts.push(text.slice(last, index));
    const location = source.page != null ? `page ${source.page}` : "no page";
    parts.push(
      <button
        key={`cite-${index}`}
        type="button"
        onClick={() => onCite(n)}
        title={`${source.filename}, ${location}`}
        aria-label={`Source ${n}: ${source.filename}, ${location}`}
        className={`mx-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-md px-1.5 align-text-top text-[11px] font-semibold transition ${
          activeSource === n
            ? "bg-violet-500 text-white"
            : "bg-violet-500/15 text-violet-300 hover:bg-violet-500/30"
        }`}
      >
        {n}
      </button>,
    );
    last = index + match[0].length;
  }
  parts.push(text.slice(last));

  return <p className="whitespace-pre-wrap text-sm leading-7">{parts}</p>;
}
