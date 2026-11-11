import { apiFetch } from "@/lib/api";
import type { UsageSummary, UsageToday } from "@/types/usage";

async function getJson<T>(path: string): Promise<T> {
  const response = await apiFetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error("Could not load your usage.");
  return (await response.json()) as T;
}

export const fetchUsageToday = () => getJson<UsageToday>("/usage/today");

export const fetchUsageSummary = (days: 7 | 30) =>
  getJson<UsageSummary>(`/usage/summary?days=${days}`);

/** The colour of a limit's bar: amber from 80 %, red at 100 %. */
export function limitTone(percent: number | null): "ok" | "warn" | "full" {
  if (percent === null) return "ok";
  if (percent >= 100) return "full";
  return percent >= 80 ? "warn" : "ok";
}

/** 1,234 → "1.2k", 2,500,000 → "2.5M"; small numbers as they are. */
export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

/** An amount of money: two decimals, four for very small amounts so they don't show as 0. */
export function formatMoney(value: number, currency: string): string {
  const digits = value > 0 && value < 0.01 ? 4 : 2;
  return `${value.toFixed(digits)} ${currency}`;
}

/** 18,720 s → "5h 12m"; 3,000 s → "50m"; under a minute → "<1m". */
export function formatResetsIn(seconds: number): string {
  const minutes = Math.max(0, Math.floor(seconds / 60));
  if (minutes < 1) return "<1m";
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours}h${minutes % 60 ? ` ${minutes % 60}m` : ""}` : `${minutes}m`;
}

/** The "62 / 100 questions today (62%) · resets in 5h" line. */
export function describeChatLimit(
  used: number,
  limit: number,
  percent: number | null,
  resetsInSeconds: number,
): string {
  const word = (n: number) => (n === 1 ? "question" : "questions");
  if (limit === 0) return `${used} ${word(used)} today · no limit`;
  return `${used} / ${limit} ${word(limit)} today (${percent ?? 0}%) · resets in ${formatResetsIn(resetsInSeconds)}`;
}
