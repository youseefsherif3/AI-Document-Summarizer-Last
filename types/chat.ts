export type ChatMessageRole = "user" | "assistant";

// streaming: the answer is still arriving; stopped: the user pressed Stop.
export type ChatMessageStatus = "streaming" | "done" | "stopped" | "error";

/** One numbered source of an answer, cited in the text as [n]. */
export interface ChatSource {
  n: number;
  documentId: number;
  filename: string;
  page: number | null;
  chunkId: number;
  snippet: string;
  similarity: number;
}

export interface ChatMessage {
  id: string;
  role: ChatMessageRole;
  content: string;
  createdAt: Date;
  // Set on answers from /chat/stream (not on posted summaries).
  sources?: ChatSource[];
  status?: ChatMessageStatus;
  error?: string;
}
