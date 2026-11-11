import { limitTone } from "@/lib/usage-api";

const BAR_COLOR = { ok: "bg-violet-500", warn: "bg-amber-400", full: "bg-red-500" } as const;

/** A progress bar for a daily limit: violet, amber from 80 %, red at 100 %. */
export default function LimitBar({
  percent,
  label,
  thin = false,
}: {
  percent: number | null;
  label: string;
  thin?: boolean;
}) {
  const tone = limitTone(percent);
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent ?? 0}
      className={`w-full overflow-hidden rounded-full bg-white/10 ${thin ? "h-1" : "h-2"}`}
    >
      <div
        className={`h-full rounded-full transition-all duration-500 ${BAR_COLOR[tone]}`}
        style={{ width: `${percent ?? 0}%` }}
      />
    </div>
  );
}
