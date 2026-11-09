import { FileText, CheckCircle2, Clock, AlertCircle } from "lucide-react";
import type { DashboardData } from "@/types/dashboard";

export default function StatsCards({ data }: { data: DashboardData | null }) {
  const stats = [
    { label: "Total Documents", value: data?.total_documents, icon: FileText, color: "text-blue-400", bg: "bg-blue-400/10" },
    { label: "Completed", value: data?.completed_documents, icon: CheckCircle2, color: "text-emerald-400", bg: "bg-emerald-400/10" },
    { label: "Processing", value: data?.processing_documents, icon: Clock, color: "text-amber-400", bg: "bg-amber-400/10" },
    { label: "Failed", value: data?.failed_documents, icon: AlertCircle, color: "text-red-400", bg: "bg-red-400/10" },
  ];

  return (
    <>
      <section className="mb-5 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {stats.map((stat) => {
          const Icon = stat.icon;
          return (
            <div key={stat.label} className="rounded-2xl border border-white/10 bg-[#111827] p-5">
              <div className="flex items-center justify-between">
                <p className="text-sm text-gray-400">{stat.label}</p>
                <div className={`rounded-xl p-2 ${stat.bg}`}><Icon size={20} className={stat.color} /></div>
              </div>
              <p className="mt-4 text-3xl font-bold text-white">{stat.value ?? "—"}</p>
            </div>
          );
        })}
      </section>
      <div className="mb-8 flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-white/10 bg-[#111827] px-5 py-3 text-sm">
        <span className="text-gray-400">File types</span>
        {data && Object.keys(data.file_types).length === 0 && <span className="text-gray-500">No uploads yet</span>}
        {Object.entries(data?.file_types ?? {}).map(([type, count]) => (
          <span key={type} className="text-gray-300"><span className="font-medium text-white">{type.toUpperCase()}</span> {count}</span>
        ))}
      </div>
    </>
  );
}
