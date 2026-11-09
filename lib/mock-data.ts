import type { UploadedDocument } from "@/types/document";

export const mockDocuments: UploadedDocument[] = [
  {
    id: "doc-001",
    file: new File([], "annual-report.pdf", {
      type: "application/pdf",
    }),
    name: "annual-report.pdf",
    size: 2.4 * 1024 * 1024,
    type: "pdf",
    status: "completed",
    progress: 100,
    summary: "This document provides an overview of the main topics, key points, and conclusions.",
  },
  {
    id: "doc-002",
    file: new File([], "meeting-notes.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }),
    name: "meeting-notes.docx",
    size: 512 * 1024,
    type: "docx",
    status: "summarizing",
    progress: 65,
  },
];
