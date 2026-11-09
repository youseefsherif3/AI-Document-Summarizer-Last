export type DocumentStatus =
  | "pending"
  | "extracting"
  | "summarizing"
  | "completed"
  | "failed";

export type DocumentType = "pdf" | "docx" | "txt";

export interface UploadedDocument {
  id: string;
  backendId?: number;
  file?: File;
  name: string;
  size: number;
  type: DocumentType;
  status: DocumentStatus;
  progress: number;
  summary?: string;
  createdAt?: string;
  error?: string;
}
