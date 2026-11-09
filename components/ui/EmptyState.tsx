import type { LucideIcon } from "lucide-react";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
}

export default function EmptyState({
  icon: Icon,
  title,
  description,
}: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-white/10 bg-[#111827] px-6 py-12 text-center">
      <div className="mb-4 rounded-2xl bg-violet-500/10 p-4">
        <Icon size={28} className="text-violet-400" />
      </div>

      <h3 className="text-lg font-semibold text-white">{title}</h3>

      <p className="mt-2 max-w-sm text-sm text-gray-500">{description}</p>
    </div>
  );
}
