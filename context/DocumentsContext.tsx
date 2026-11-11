
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
import { useAuth } from "@/context/AuthContext";
import { apiFetch } from "@/lib/api";
import { userUploadPrefix } from "@/lib/upload-paths";

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

// Queue ingestion (the backend answers an upload with 202 and a job id): how often the job is
// checked, and how many checks in a row may fail (network trouble) before giving up.
const JOB_POLL_INTERVAL_MS = 1500;
const JOB_POLL_MAX_FAILURES = 5;

interface BackendJob {
  status: "pending" | "processing" | "completed" | "failed";
  document_id: number | null;
  summary: string | null;
  error: { code: string; message: string } | null;
}

/** Wait, but stop with an AbortError as soon as the signal is aborted. */
const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });

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
  const { user } = useAuth();
  const userId = user?.id;

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
    const response = await apiFetch("/documents", { cache: "no-store" });
    if (!response.ok) throw new Error("Failed to retrieve saved documents.");
    const saved = mapBackendDocuments(await response.json());
    // Keep uploads that are still running; they are not in the database yet.
    setDocuments((current) => [...saved, ...current.filter(isUploading)]);
  };

  // Load the signed-in user's saved documents. Signing out reloads the page, so documents
  // from one account never stay in memory for the next.
  useEffect(() => {
    if (userId === undefined) return;
    let isMounted = true;

    const fetchDocuments = async () => {
      try {
        const response = await apiFetch("/documents");

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
  }, [userId]);

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

  // Queue ingestion: check the job until it ends, showing "Pending" while it waits in the
  // queue and "Summarizing" while the worker has it. The whole summary arrives at the end.
  const waitForJob = async (
    id: string,
    jobId: string,
    signal: AbortSignal,
  ) => {
    let failures = 0;
    while (true) {
      await sleep(JOB_POLL_INTERVAL_MS, signal);
      let job: BackendJob;
      try {
        const response = await apiFetch(`/jobs/${jobId}`, {
          cache: "no-store",
          signal,
        });
        if (response.status === 404) throw new Error("The upload job was not found.");
        if (!response.ok) throw new Error("Could not check the upload.");
        job = await response.json();
        failures = 0;
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") throw err;
        if (err instanceof Error && err.message === "The upload job was not found.") throw err;
        failures += 1;
        if (failures >= JOB_POLL_MAX_FAILURES) {
          throw new Error("Could not check the upload. Refresh the page to see its result.");
        }
        continue;
      }

      if (job.status === "completed") {
        updateDocument(id, {
          backendId: job.document_id ?? undefined,
          status: "completed",
          progress: 100,
          summary: job.summary ?? undefined,
        });
        return;
      }
      if (job.status === "failed") {
        throw new Error(job.error?.message ?? "Failed to process the document.");
      }
      updateDocument(
        id,
        job.status === "processing"
          ? { status: "summarizing", progress: 75 }
          : { status: "pending", progress: 55 },
      );
    }
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
        // Uploads go to the signed-in user's own Blob folder; the token route and the
        // backend refuse any other path.
        if (userId === undefined) throw new Error("Please sign in again to upload.");
        const blob = await uploadBlob(
          `${userUploadPrefix(userId)}${document.id}.${extension}`,
          document.file,
          {
            access: "private",
            handleUploadUrl: "/api/document-uploads",
            clientPayload: JSON.stringify({ documentId: document.id }),
            abortSignal: controller.signal,
          },
        );
        updateDocument(document.id, { progress: 50 });
        response = await apiFetch("/documents/upload/stream/blob", {
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
        response = await apiFetch("/documents/upload/stream", {
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

      if (response.status === 202) {
        // INGESTION_MODE=queue on the backend: a worker processes the file.
        const { job_id: jobId } = (await response.json()) as { job_id: string };
        updateDocument(document.id, { status: "pending", progress: 55 });
        await waitForJob(document.id, jobId, controller.signal);
        window.dispatchEvent(new Event("documents-updated"));
        await refreshDocuments();
        return;
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
      const response = await apiFetch(
        `/documents/${document.backendId}`,
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
