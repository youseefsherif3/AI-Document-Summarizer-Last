
"use client";

import {
  createContext,
  useContext,
  useEffect,
  useState,
  useRef,
  type ReactNode,
} from "react";

import type { UploadedDocument, DocumentType } from "@/types/document";
import { upload as uploadBlob } from "@vercel/blob/client";
import { apiUrl } from "@/lib/api";

import {
  ALLOWED_EXTENSIONS,
  MAX_FILE_SIZE,
  MAX_CONCURRENT_UPLOADS,
} from "@/lib/constants";

const UPLOAD_LIMIT_MESSAGE = `You can upload up to ${MAX_CONCURRENT_UPLOADS} files at a time. Wait for the current uploads to finish.`;

// Uploads that are still running and use one of the concurrent upload slots.
const isUploading = (document: UploadedDocument) =>
  document.status === "pending" ||
  document.status === "extracting" ||
  document.status === "summarizing";

interface DocumentsContextType {
  documents: UploadedDocument[];
  error: string | null;
  remainingSlots: number;
  addFiles: (files: File[]) => void;
  removeDocument: (id: string) => void;
  updateDocument: (id: string, updates: Partial<UploadedDocument>) => void;
  cancelUpload: (id: string) => void;
}

interface BackendDocument {
  id: number;
  filename: string;
  file_type: string;
  file_size: number;
  summary: string | null;
  created_at: string;
}

const DocumentsContext = createContext<DocumentsContextType | undefined>(
  undefined,
);

export function DocumentsProvider({ children }: { children: ReactNode }) {
  const [documents, setDocuments] = useState<UploadedDocument[]>([]);
  const [error, setError] = useState<string | null>(null);
  const controllers = useRef(new Map<string, AbortController>());

  const mapBackendDocuments = (data: BackendDocument[]): UploadedDocument[] =>
    data.map((document) => ({
      id: String(document.id),
      backendId: document.id,
      name: document.filename,
      size: document.file_size,
      type: document.file_type as DocumentType,
      status: document.summary ? "completed" : "failed",
      progress: document.summary ? 100 : 0,
      summary: document.summary ?? undefined,
      createdAt: document.created_at,
    }));

  const refreshDocuments = async () => {
    const response = await fetch(apiUrl("/documents"), { cache: "no-store" });
    if (!response.ok) throw new Error("Failed to retrieve saved documents.");
    const saved = mapBackendDocuments(await response.json());
    // Keep uploads that are still running; they are not in the database yet.
    setDocuments((current) => [...saved, ...current.filter(isUploading)]);
  };

  // Restore saved documents when the application starts.
  useEffect(() => {
    let isMounted = true;

    const fetchDocuments = async () => {
      try {
        const response = await fetch(apiUrl("/documents"));

        if (!response.ok) {
          throw new Error("Failed to retrieve saved documents.");
        }

        const data: BackendDocument[] = await response.json();

        if (!isMounted) return;

        setDocuments(mapBackendDocuments(data));
      } catch (err) {
        if (!isMounted) return;

        setError(
          err instanceof Error
            ? err.message
            : "Failed to load saved documents.",
        );
      }
    };

    void fetchDocuments();

    return () => {
      isMounted = false;
    };
  }, []);

  const updateDocument = (
    id: string,
    updates: Partial<UploadedDocument>,
  ) => {
    setDocuments((current) =>
      current.map((document) =>
        document.id === id ? { ...document, ...updates } : document,
      ),
    );
  };

  // Upload a document to the backend.
  const uploadDocument = async (document: UploadedDocument) => {
    updateDocument(document.id, {
      status: "summarizing",
      progress: 50,
    });

    try {
      if (!document.file) {
        throw new Error("Original file is not available.");
      }

      const controller = new AbortController();
      controllers.current.set(document.id, controller);
      const useVercelBlob =
        process.env.NEXT_PUBLIC_VERCEL_BLOB_UPLOADS === "true";
      let response: Response;

      if (useVercelBlob) {
        const extension = document.name.split(".").pop()?.toLowerCase();
        const blob = await uploadBlob(
          `documind/uploads/${document.id}.${extension}`,
          document.file,
          {
            access: "private",
            handleUploadUrl: "/api/document-uploads",
            clientPayload: JSON.stringify({ documentId: document.id }),
            abortSignal: controller.signal,
          },
        );
        updateDocument(document.id, { progress: 50 });
        response = await fetch(apiUrl("/documents/upload/stream/blob"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            pathname: blob.pathname,
            filename: document.file.name,
            file_size: document.file.size,
          }),
          signal: controller.signal,
        });
      } else {
        const formData = new FormData();
        formData.append("file", document.file);
        response = await fetch(apiUrl("/documents/upload/stream"), {
          method: "POST",
          body: formData,
          signal: controller.signal,
        });
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "Failed to process the document.",
        );
      }

      if (!response.body) throw new Error("The summary stream was unavailable.");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let streamedSummary = "";
      let completed = false;
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const line = frame.split("\n").find((part) => part.startsWith("data: "));
          if (!line) continue;
          const event = JSON.parse(line.slice(6)) as {
            type: string; content?: string; summary?: string;
            document_id?: number; message?: string;
          };
          if (event.type === "delta") {
            streamedSummary += event.content ?? "";
            updateDocument(document.id, { summary: streamedSummary });
          } else if (event.type === "done") {
            completed = true;
            updateDocument(document.id, {
              backendId: event.document_id, status: "completed", progress: 100,
              summary: event.summary,
            });
          } else if (event.type === "error") {
            throw new Error(event.message ?? "Failed to summarize the document.");
          }
        }
        if (done) break;
      }
      if (!completed) throw new Error("The summary stream ended unexpectedly.");
      window.dispatchEvent(new Event("documents-updated"));
      await refreshDocuments();
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      updateDocument(document.id, {
        status: "failed",
        progress: 0,
        summary: undefined,
        error:
          err instanceof Error
            ? err.message
            : "An unexpected error occurred.",
      });
    } finally {
      controllers.current.delete(document.id);
    }
  };

  // Add and validate selected files.
  const addFiles = (files: File[]) => {
    setError(null);

    const newDocuments: UploadedDocument[] = [];
    const rejectedMessages: string[] = [];

    const remaining =
      MAX_CONCURRENT_UPLOADS - documents.filter(isUploading).length;

    if (remaining <= 0) {
      setError(UPLOAD_LIMIT_MESSAGE);
      return;
    }

    for (const file of files) {
      if (newDocuments.length >= remaining) {
        rejectedMessages.push(UPLOAD_LIMIT_MESSAGE);
        break;
      }

      const extension = `.${file.name.split(".").pop()?.toLowerCase()}`;

      if (!ALLOWED_EXTENSIONS.includes(extension)) {
        rejectedMessages.push(`${file.name}: Unsupported file type.`);
        continue;
      }

      if (file.size > MAX_FILE_SIZE) {
        rejectedMessages.push(`${file.name}: File size exceeds 10 MB.`);
        continue;
      }

      const isDuplicate =
        documents.some(
          (document) =>
            document.name.toLowerCase() === file.name.toLowerCase() &&
            document.size === file.size,
        ) ||
        newDocuments.some(
          (document) =>
            document.name.toLowerCase() === file.name.toLowerCase() &&
            document.size === file.size,
        );

      if (isDuplicate) {
        rejectedMessages.push(`${file.name}: File already uploaded.`);
        continue;
      }

      const type = extension.slice(1) as DocumentType;

      newDocuments.push({
        id: crypto.randomUUID(),
        file,
        name: file.name,
        size: file.size,
        type,
        status: "pending",
        progress: 0,
      });
    }

    if (newDocuments.length > 0) {
      setDocuments((current) => [...current, ...newDocuments]);

      newDocuments.forEach((document) => {
        void uploadDocument(document);
      });
    }

    if (rejectedMessages.length > 0) {
      setError(rejectedMessages.join("\n"));
    }
  };

  // Delete a document from the backend and frontend.
  const removeDocument = async (id: string) => {
    const document = documents.find((item) => item.id === id);

    if (!document) return;

    if (!document.backendId && controllers.current.has(id)) {
      controllers.current.get(id)?.abort();
      controllers.current.delete(id);
      setDocuments((current) => current.filter((item) => item.id !== id));
      return;
    }

    if (!document.backendId) {
      setError(
        "This document has not been saved to the database yet.",
      );
      return;
    }

    setError(null);

    try {
      const response = await fetch(
        apiUrl(`/documents/${document.backendId}`),
        {
          method: "DELETE",
        },
      );

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "Failed to delete the document.",
        );
      }

      setDocuments((current) =>
        current.filter((item) => item.id !== id),
      );
      window.dispatchEvent(new Event("documents-updated"));
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to delete the document.",
      );
    }
  };

  return (
    <DocumentsContext.Provider
      value={{
        documents,
        error,
        remainingSlots: Math.max(
          0,
          MAX_CONCURRENT_UPLOADS - documents.filter(isUploading).length,
        ),
        addFiles,
        removeDocument,
        updateDocument,
        cancelUpload: (id) => {
          controllers.current.get(id)?.abort();
          controllers.current.delete(id);
          setDocuments((current) => current.filter((item) => item.id !== id));
        },
      }}
    >
      {children}
    </DocumentsContext.Provider>
  );
}

export function useDocumentsContext() {
  const context = useContext(DocumentsContext);

  if (!context) {
    throw new Error(
      "useDocumentsContext must be used inside DocumentsProvider",
    );
  }

  return context;
}
