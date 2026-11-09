"use client";

import { useState } from "react";
import type { UploadedDocument, DocumentType } from "@/types/document";
import {
  ALLOWED_EXTENSIONS,
  MAX_FILE_SIZE,
  MAX_FILES_PER_SESSION,
} from "@/lib/constants";

export function useDocuments() {
  const [documents, setDocuments] = useState<UploadedDocument[]>([]);
  const [error, setError] = useState<string | null>(null);

  const addFiles = (files: File[]) => {
    setError(null);

    const remainingSlots = MAX_FILES_PER_SESSION - documents.length;

    if (remainingSlots <= 0) {
      setError("You can upload a maximum of 3 files per session.");
      return;
    }

    const acceptedFiles = files.slice(0, remainingSlots);
    const newDocuments: UploadedDocument[] = [];

    for (const file of acceptedFiles) {
      const extension = `.${file.name.split(".").pop()?.toLowerCase()}`;

      if (!ALLOWED_EXTENSIONS.includes(extension)) {
        setError(`${file.name}: Unsupported file type.`);
        continue;
      }

      if (file.size > MAX_FILE_SIZE) {
        setError(`${file.name}: File size exceeds 10 MB.`);
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

    setDocuments((current) => [...current, ...newDocuments]);
  };

  const removeDocument = (id: string) => {
    setDocuments((current) => current.filter((document) => document.id !== id));
  };

  const updateDocument = (id: string, updates: Partial<UploadedDocument>) => {
    setDocuments((current) =>
      current.map((document) =>
        document.id === id ? { ...document, ...updates } : document,
      ),
    );
  };

  return {
    documents,
    error,
    addFiles,
    removeDocument,
    updateDocument,
    remainingSlots: MAX_FILES_PER_SESSION - documents.length,
  };
}
