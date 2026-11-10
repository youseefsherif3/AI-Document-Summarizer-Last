"use client";

import { useCallback, useEffect, useState } from "react";
import MainLayout from "@/components/layout/MainLayout";
import WelcomeSection from "@/components/dashboard/WelcomeSection";
import StatsCards from "@/components/dashboard/StatsCards";
import RecentActivity from "@/components/dashboard/RecentActivity";
import type { DashboardData } from "@/types/dashboard";
import { apiUrl } from "@/lib/api";

type DashboardResult = { data: DashboardData } | { error: string };

async function loadDashboard(): Promise<DashboardResult> {
  try {
    const response = await fetch(apiUrl("/dashboard"), { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load dashboard data.");
    return { data: await response.json() };
  } catch (reason) {
    return { error: reason instanceof Error ? reason.message : "Could not load dashboard data." };
  }
}

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // State is only updated once the request has finished, never synchronously in the effect.
  // On an error the last loaded data stays on screen, as before.
  const refresh = useCallback(() => {
    void loadDashboard().then((result) => {
      if ("data" in result) {
        setData(result.data);
        setError(null);
      } else {
        setError(result.error);
      }
      setLoading(false);
    });
  }, []);

  useEffect(() => {
    refresh();
    window.addEventListener("documents-updated", refresh);
    return () => window.removeEventListener("documents-updated", refresh);
  }, [refresh]);

  return (
    <MainLayout>
      <WelcomeSection />
      <StatsCards data={data} />
      <RecentActivity documents={data?.recent_documents ?? []} loading={loading} error={error} />
    </MainLayout>
  );
}
