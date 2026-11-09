import { Clock, LoaderCircle, CheckCircle2, AlertCircle } from "lucide-react";

import type { DocumentStatus } from "@/types/document";

interface FileStatusProps {
  status: DocumentStatus;
}

const statusConfig = {
  pending: {
    label: "Pending",
    icon: Clock,
    className: "bg-gray-500/10 text-gray-400",
  },
  extracting: {
    label: "Extracting",
    icon: LoaderCircle,
    className: "bg-blue-500/10 text-blue-400",
  },
  summarizing: {
    label: "Summarizing",
    icon: LoaderCircle,
    className: "bg-amber-500/10 text-amber-400",
  },
  completed: {
    label: "Completed",
    icon: CheckCircle2,
    className: "bg-emerald-500/10 text-emerald-400",
  },
  failed: {
    label: "Failed",
    icon: AlertCircle,
    className: "bg-red-500/10 text-red-400",
  },
};

export default function FileStatus({ status }: FileStatusProps) {
  const config = statusConfig[status];
  const Icon = config.icon;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium ${config.className}`}
    >
      <Icon
        size={14}
        className={
          status === "extracting" || status === "summarizing"
            ? "animate-spin"
            : ""
        }
      />
      {config.label}
    </span>
  );
}
