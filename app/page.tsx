"use client";

import { useCallback, useEffect, useState } from "react";
import MainLayout from "@/components/layout/MainLayout";
import WelcomeSection from "@/components/dashboard/WelcomeSection";
import StatsCards from "@/components/dashboard/StatsCards";
import RecentActivity from "@/components/dashboard/RecentActivity";
import type { DashboardData } from "@/types/dashboard";

export default function Home() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL;
    if (!apiUrl) {
      setError("Backend API URL is not configured.");
      setLoading(false);
      return;
    }
    try {
      const response = await fetch(`${apiUrl}/dashboard`, { cache: "no-store" });
      if (!response.ok) throw new Error("Could not load dashboard data.");
      setData(await response.json());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load dashboard data.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
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
