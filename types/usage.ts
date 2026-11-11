// The backend's /usage/today and /usage/summary (backend/app/usage_report.py).

export interface LimitUsage {
  used: number;
  /** 0 means no limit. */
  limit: number;
  unlimited: boolean;
  remaining: number | null;
  percent: number | null;
}

export interface UsageToday {
  day: string;
  uploads: LimitUsage;
  chat: LimitUsage;
  resets_at: string;
  resets_in_seconds: number;
}

export interface TokenTotals {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost: number;
}

export interface UsageDay extends TokenTotals {
  date: string;
  tokens_by_kind: Record<string, number>;
}

export interface UsageCall {
  time: string;
  kind: string;
  model: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  outcome: string;
}

export interface UsageSummary {
  days: 7 | 30;
  from: string;
  to: string;
  currency: string;
  totals: TokenTotals & { errors: number; unpriced_calls: number };
  by_day: UsageDay[];
  by_kind: Record<string, TokenTotals>;
  recent_calls: UsageCall[];
}
