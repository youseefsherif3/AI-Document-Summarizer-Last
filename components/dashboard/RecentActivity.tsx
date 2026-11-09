import { FileText, CheckCircle2, Clock } from "lucide-react";
import type { DashboardDocument } from "@/types/dashboard";

function formatDate(value: string) {
  const date = new Date(value.endsWith("Z") ? value : `${value.replace(" ", "T")}Z`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export default function RecentActivity({ documents, loading, error }: {
  documents: DashboardDocument[];
  loading: boolean;
  error: string | null;
}) {
  return (
    <section className="rounded-2xl border border-white/10 bg-[#111827] p-4 sm:p-6">
      <div className="mb-6">
        <h2 className="text-lg font-semibold text-white">Recent Activity</h2>
        <p className="mt-1 text-sm text-gray-500">Track the latest document updates</p>
      </div>
      {error ? <p role="alert" className="text-sm text-red-400">{error}</p> : loading ? (
        <p className="text-sm text-gray-500">Loading recent documents…</p>
      ) : documents.length === 0 ? (
        <p className="text-sm text-gray-500">Your recent uploads will appear here.</p>
      ) : (
        <div className="space-y-5">
          {documents.map((document) => {
            const complete = Boolean(document.summary);
            const Icon = complete ? CheckCircle2 : Clock;
            const color = complete ? "text-emerald-400" : "text-amber-400";
            return (
              <div key={document.id} className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                <div className="flex min-w-0 items-center gap-3">
                  <div className="rounded-xl bg-white/5 p-3"><FileText size={19} className="text-gray-400" /></div>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-white">{document.filename}</p>
                    <p className="mt-1 text-xs text-gray-500">{document.file_type.toUpperCase()} · {complete ? "Summary completed" : "Summary unavailable"}</p>
                  </div>
                </div>
                <div className="shrink-0 pl-14 sm:pl-0 sm:text-right">
                  <div className={`flex items-center gap-1.5 sm:justify-end ${color}`}><Icon size={14} /><span className="text-xs">{complete ? "Completed" : "Incomplete"}</span></div>
                  <p className="mt-1 text-xs text-gray-500">{formatDate(document.created_at)}</p>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
