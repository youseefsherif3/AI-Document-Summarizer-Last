"use client";

import { useCallback, useEffect, useState } from "react";
import { Coins, FileUp, MessageSquare, Sigma, Activity } from "lucide-react";

import MainLayout from "@/components/layout/MainLayout";
import EmptyState from "@/components/ui/EmptyState";
import LimitCard from "@/components/usage/LimitCard";
import RecentCalls from "@/components/usage/RecentCalls";
import TokensChart from "@/components/usage/TokensChart";
import { formatMoney, formatTokens, fetchUsageSummary, fetchUsageToday } from "@/lib/usage-api";
import type { UsageSummary, UsageToday } from "@/types/usage";

type Period = 7 | 30;

function Stat({ label, value, hint, icon: Icon }: {
  label: string;
  value: string;
  hint?: string;
  icon: typeof Sigma;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-[#111827] p-4 sm:p-5">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-gray-400">{label}</p>
        <div className="rounded-xl bg-white/5 p-2"><Icon size={20} className="text-gray-400" /></div>
      </div>
      <p className="mt-4 break-words text-2xl font-bold text-white sm:text-3xl">{value}</p>
      {hint && <p className="mt-1 text-xs text-gray-500">{hint}</p>}
    </div>
  );
}

export default function UsagePage() {
  const [days, setDays] = useState<Period>(7);
  const [today, setToday] = useState<UsageToday | null>(null);
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // State is only set once a request has finished, never synchronously in the effect.
  const load = useCallback((period: Period) => {
    Promise.all([fetchUsageToday(), fetchUsageSummary(period)])
      .then(([todayData, summaryData]) => {
        setToday(todayData);
        setSummary(summaryData);
        setError(null);
      })
      .catch((reason) => {
        setError(reason instanceof Error ? reason.message : "Could not load your usage.");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load(days);
  }, [days, load]);

  const switchPeriod = (period: Period) => {
    if (period === days) return;
    setLoading(true);
    setDays(period);
  };

  const todayTokens = summary?.by_day.at(-1)?.total_tokens ?? 0;
  const hasUsage = (summary?.totals.calls ?? 0) > 0;
  const currency = summary?.currency ?? "EGP";

  return (
    <MainLayout>
      <div className="mx-auto max-w-6xl space-y-6 sm:space-y-8">
        <section className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-2xl font-bold text-white">Usage</h1>
            <p className="mt-2 text-sm text-gray-400">
              Your daily limits and the AI work done for your documents.
            </p>
          </div>

          <div role="group" aria-label="Period" className="inline-flex shrink-0 self-start rounded-xl border border-white/10 bg-[#111827] p-1 sm:self-auto">
            {([7, 30] as const).map((period) => (
              <button
                key={period}
                type="button"
                onClick={() => switchPeriod(period)}
                aria-pressed={days === period}
                className={`rounded-lg px-4 py-1.5 text-sm font-medium transition ${
                  days === period ? "bg-violet-600 text-white" : "text-gray-400 hover:text-white"
                }`}
              >
                {period} days
              </button>
            ))}
          </div>
        </section>

        {error && (
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-500/20 bg-red-500/5 p-4">
            <p className="text-sm text-red-400">{error}</p>
            <button
              type="button"
              onClick={() => load(days)}
              className="rounded-lg border border-white/15 px-3 py-1.5 text-sm text-gray-200 transition hover:bg-white/5"
            >
              Try again
            </button>
          </div>
        )}

        {/* Today's limits */}
        <section aria-label="Today's limits" className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <LimitCard
            title="Uploads today"
            icon={FileUp}
            usage={today?.uploads ?? null}
            resetsInSeconds={today?.resets_in_seconds ?? null}
          />
          <LimitCard
            title="Chat questions today"
            icon={MessageSquare}
            usage={today?.chat ?? null}
            resetsInSeconds={today?.resets_in_seconds ?? null}
          />
        </section>

        {/* Tokens and cost */}
        <section aria-label="Tokens and cost" className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Stat label="Tokens today" value={summary ? formatTokens(todayTokens) : "—"} icon={Sigma} />
          <Stat
            label={`Tokens, last ${days} days`}
            value={summary ? formatTokens(summary.totals.total_tokens) : "—"}
            hint={
              summary
                ? `${formatTokens(summary.totals.prompt_tokens)} in · ${formatTokens(summary.totals.completion_tokens)} out · ${summary.totals.calls} calls`
                : undefined
            }
            icon={Activity}
          />
          <Stat
            label={`Estimated cost, last ${days} days`}
            value={summary ? formatMoney(summary.totals.cost, currency) : "—"}
            hint={
              summary && summary.totals.unpriced_calls > 0
                ? `${summary.totals.unpriced_calls} calls use a model with no price, so this is incomplete.`
                : "An estimate from list prices per million tokens."
            }
            icon={Coins}
          />
        </section>

        {loading && !summary ? (
          <p className="text-sm text-gray-500">Loading your usage…</p>
        ) : summary && !hasUsage ? (
          <EmptyState
            icon={Activity}
            title="No AI usage yet"
            description={`Upload a document or ask a question in Chat, and your tokens and calls will show up here (last ${days} days).`}
          />
        ) : summary ? (
          <>
            <section className="rounded-2xl border border-white/10 bg-[#111827] p-4 sm:p-6">
              <h2 className="text-lg font-semibold text-white">Tokens per day</h2>
              <p className="mb-5 mt-1 text-sm text-gray-500">
                Input and output tokens of every AI call, by UTC day.
              </p>
              <TokensChart days={summary.by_day} />
            </section>

            <section className="rounded-2xl border border-white/10 bg-[#111827] p-4 sm:p-6">
              <h2 className="text-lg font-semibold text-white">Recent AI calls</h2>
              <p className="mb-4 mt-1 text-sm text-gray-500">The last {summary.recent_calls.length} calls.</p>
              <RecentCalls calls={summary.recent_calls} />
            </section>
          </>
        ) : null}
      </div>
    </MainLayout>
  );
}
