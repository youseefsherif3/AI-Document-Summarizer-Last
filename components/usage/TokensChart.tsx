import { formatTokens } from "@/lib/usage-api";
import type { UsageDay } from "@/types/usage";

// One colour per kind of AI call, in stacking order (bottom first).
const KINDS = [
  { key: "chat", label: "Chat", bar: "bg-violet-500" },
  { key: "summary", label: "Summaries", bar: "bg-sky-400" },
  { key: "embedding", label: "Embeddings", bar: "bg-emerald-400" },
] as const;
const OTHER = { key: "other", label: "Other", bar: "bg-gray-500" };

function segments(day: UsageDay) {
  const known = KINDS.map((kind) => ({ ...kind, tokens: day.tokens_by_kind[kind.key] ?? 0 }));
  const knownTotal = known.reduce((sum, item) => sum + item.tokens, 0);
  return [...known, { ...OTHER, tokens: Math.max(0, day.total_tokens - knownTotal) }].filter(
    (item) => item.tokens > 0,
  );
}

function shortDate(iso: string) {
  const [, month, day] = iso.split("-");
  return `${Number(day)}/${Number(month)}`;
}

/** Tokens per day as stacked bars (chat, summaries, embeddings). Plain divs: no chart library. */
export default function TokensChart({ days }: { days: UsageDay[] }) {
  const max = Math.max(1, ...days.map((day) => day.total_tokens));
  const labelEvery = days.length > 10 ? 5 : 1;
  const total = days.reduce((sum, day) => sum + day.total_tokens, 0);

  return (
    <div>
      <div
        role="img"
        aria-label={`Tokens per day over the last ${days.length} days, ${formatTokens(total)} in total`}
        className="flex h-44 items-stretch gap-1 border-b border-white/10 sm:h-52"
      >
        {days.map((day) => (
          <div
            key={day.date}
            title={`${day.date}: ${day.total_tokens.toLocaleString()} tokens`}
            className="flex min-w-0 flex-1 flex-col justify-end"
          >
            <div
              className="flex flex-col-reverse overflow-hidden rounded-t-sm"
              style={{ height: `${(day.total_tokens / max) * 100}%` }}
            >
              {segments(day).map((item) => (
                <div
                  key={item.key}
                  className={item.bar}
                  style={{ height: `${(item.tokens / day.total_tokens) * 100}%` }}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* Dates under the bars: every day for a week, every fifth day for a month */}
      <div className="mt-1 flex gap-1" aria-hidden="true">
        {days.map((day, index) => (
          <span key={day.date} className="min-w-0 flex-1 text-center text-[10px] text-gray-500">
            {index % labelEvery === 0 || index === days.length - 1 ? (
              <span className="whitespace-nowrap">{shortDate(day.date)}</span>
            ) : null}
          </span>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-xs text-gray-400">
        <ul className="flex flex-wrap gap-x-4 gap-y-1">
          {KINDS.map((kind) => (
            <li key={kind.key} className="flex items-center gap-1.5">
              <span className={`inline-block size-2.5 rounded-sm ${kind.bar}`} />
              {kind.label}
            </li>
          ))}
        </ul>
        <span>Tallest day: {formatTokens(total === 0 ? 0 : max)} tokens</span>
      </div>
    </div>
  );
}
