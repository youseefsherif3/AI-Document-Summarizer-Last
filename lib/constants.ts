// How many files can upload and summarize at the same time.
// Stored documents are not limited.
export const MAX_CONCURRENT_UPLOADS = 3;

export const ALLOWED_FILE_TYPES = {
  PDF: "application/pdf",
  DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  TXT: "text/plain",
} as const;

export const ALLOWED_EXTENSIONS = [".pdf", ".docx", ".txt"];

export const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB

export const APP_NAME = "DocuMind";

export const APP_DESCRIPTION =
  "Transform your documents into clear AI-powered summaries.";
