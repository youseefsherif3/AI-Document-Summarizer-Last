import type { LucideIcon } from "lucide-react";

import LimitBar from "@/components/usage/LimitBar";
import { formatResetsIn, limitTone } from "@/lib/usage-api";
import type { LimitUsage } from "@/types/usage";

const PERCENT_COLOR = { ok: "text-gray-300", warn: "text-amber-400", full: "text-red-400" } as const;

export default function LimitCard({
  title,
  icon: Icon,
  usage,
  resetsInSeconds,
}: {
  title: string;
  icon: LucideIcon;
  usage: LimitUsage | null;
  resetsInSeconds: number | null;
}) {
  const tone = limitTone(usage?.percent ?? null);
  return (
    <div className="rounded-2xl border border-white/10 bg-[#111827] p-4 sm:p-5">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-gray-400">{title}</p>
        <div className="rounded-xl bg-violet-500/10 p-2">
          <Icon size={20} className="text-violet-400" />
        </div>
      </div>

      {usage === null ? (
        <p className="mt-4 text-3xl font-bold text-white">—</p>
      ) : (
        <>
          <p className="mt-4 flex flex-wrap items-baseline gap-x-2 text-3xl font-bold text-white">
            {usage.used}
            <span className="text-lg font-medium text-gray-500">
              {usage.unlimited ? "used · no limit" : `/ ${usage.limit}`}
            </span>
            {!usage.unlimited && (
              <span className={`text-sm font-medium ${PERCENT_COLOR[tone]}`}>{usage.percent}%</span>
            )}
          </p>
          {!usage.unlimited && (
            <div className="mt-3">
              <LimitBar percent={usage.percent} label={`${title} used today`} />
            </div>
          )}
          <p className="mt-3 text-xs text-gray-500">
            {usage.unlimited
              ? "No daily limit on this account."
              : tone === "full"
                ? "Limit reached."
                : `${usage.remaining} left today.`}
            {resetsInSeconds !== null && ` Resets in ${formatResetsIn(resetsInSeconds)} (midnight UTC).`}
          </p>
        </>
      )}
    </div>
  );
}
