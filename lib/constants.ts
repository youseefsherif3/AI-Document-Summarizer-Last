export const MAX_FILES_PER_SESSION = 3;

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
