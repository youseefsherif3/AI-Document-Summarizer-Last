export interface DashboardDocument {
  id: number;
  filename: string;
  file_type: string;
  file_size: number;
  summary: string | null;
  created_at: string;
}

export interface DashboardData {
  total_documents: number;
  completed_documents: number;
  processing_documents: number;
  failed_documents: number;
  file_types: Record<string, number>;
  recent_documents: DashboardDocument[];
}
