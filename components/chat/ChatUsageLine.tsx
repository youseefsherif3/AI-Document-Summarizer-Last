"use client";

import { useEffect, useState } from "react";

import LimitBar from "@/components/usage/LimitBar";
import { describeChatLimit, fetchUsageToday, limitTone } from "@/lib/usage-api";
import type { UsageToday } from "@/types/usage";

const TEXT_COLOR = { ok: "text-gray-500", warn: "text-amber-400", full: "text-red-400" } as const;

/**
 * "62 / 100 questions today (62%) · resets in 5h" with a thin bar, under the message box.
 * `refreshKey` changes after each answer (and after a daily-limit error), which reloads it.
 * If the numbers can't be loaded the line is simply not shown: chat works without it.
 */
export default function ChatUsageLine({ refreshKey }: { refreshKey: number }) {
  const [usage, setUsage] = useState<UsageToday | null>(null);

  useEffect(() => {
    let current = true;
    fetchUsageToday()
      .then((data) => {
        if (current) setUsage(data);
      })
      .catch(() => {
        // Keep the last numbers (or none): this line is only information.
      });
    return () => {
      current = false;
    };
  }, [refreshKey]);

  if (!usage) return null;
  const { chat } = usage;
  const tone = limitTone(chat.percent);

  return (
    <div className="mt-2 shrink-0 px-1" aria-live="polite">
      <p className={`text-center text-xs ${TEXT_COLOR[tone]}`}>
        {describeChatLimit(chat.used, chat.limit, chat.percent, usage.resets_in_seconds)}
      </p>
      {!chat.unlimited && (
        <div className="mx-auto mt-1.5 max-w-xs">
          <LimitBar percent={chat.percent} label="Chat questions used today" thin />
        </div>
      )}
    </div>
  );
}
