import type { UsageCall } from "@/types/usage";
import { formatTokens } from "@/lib/usage-api";

const KIND_LABEL: Record<string, string> = {
  chat: "Chat",
  summary: "Summary",
  embedding: "Embedding",
};

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

const tokens = (value: number | null) => (value === null ? "—" : formatTokens(value));

export default function RecentCalls({ calls }: { calls: UsageCall[] }) {
  return (
    <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <table className="w-full min-w-[34rem] text-left text-sm">
        <thead>
          <tr className="border-b border-white/10 text-xs uppercase tracking-wide text-gray-500">
            <th scope="col" className="py-2 pr-3 font-medium">Time</th>
            <th scope="col" className="py-2 pr-3 font-medium">Kind</th>
            <th scope="col" className="py-2 pr-3 font-medium">Model</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">In</th>
            <th scope="col" className="py-2 pr-3 text-right font-medium">Out</th>
            <th scope="col" className="py-2 font-medium">Result</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((call, index) => (
            <tr key={`${call.time}-${index}`} className="border-b border-white/5 last:border-0">
              <td className="whitespace-nowrap py-2.5 pr-3 text-gray-300">{formatTime(call.time)}</td>
              <td className="py-2.5 pr-3 text-gray-300">{KIND_LABEL[call.kind] ?? call.kind}</td>
              <td className="max-w-40 truncate py-2.5 pr-3 text-gray-400" title={call.model}>{call.model}</td>
              <td className="py-2.5 pr-3 text-right tabular-nums text-gray-300">{tokens(call.prompt_tokens)}</td>
              <td className="py-2.5 pr-3 text-right tabular-nums text-gray-300">{tokens(call.completion_tokens)}</td>
              <td className="py-2.5">
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                    call.outcome === "ok"
                      ? "bg-emerald-500/10 text-emerald-400"
                      : "bg-red-500/10 text-red-400"
                  }`}
                >
                  {call.outcome === "ok" ? "OK" : call.outcome}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
