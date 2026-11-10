import { apiFetch } from "@/lib/api";
import { readSseEvents } from "@/lib/sse";
import type { ChatMessageRole, ChatSource } from "@/types/chat";

/** A chat request that failed, with a message that can be shown to the user. */
export class ChatRequestError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "ChatRequestError";
    this.status = status;
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException || error instanceof Error) &&
    error.name === "AbortError"
  );
}

interface BackendSource {
  n: number;
  document_id: number;
  filename: string;
  page: number | null;
  chunk_id: number;
  snippet: string;
  similarity: number;
}

export interface ChatStreamRequest {
  message: string;
  history: { role: ChatMessageRole; content: string }[];
  // Empty = search all documents.
  documentIds: number[];
  signal: AbortSignal;
}

export interface ChatStreamHandlers {
  onSources: (sources: ChatSource[]) => void;
  onDelta: (text: string) => void;
}

/**
 * Ask /chat/stream a question. Calls `onSources` once, then `onDelta` for each piece
 * of the answer. Resolves after `done`; throws ChatRequestError on any failure, or an
 * AbortError if the request was stopped through `signal`.
 */
export async function streamChatAnswer(
  { message, history, documentIds, signal }: ChatStreamRequest,
  { onSources, onDelta }: ChatStreamHandlers,
): Promise<void> {
  let response: Response;
  try {
    response = await apiFetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history,
        document_ids: documentIds.length > 0 ? documentIds : null,
      }),
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ChatRequestError(
      "Could not reach the server. Check that the backend is running.",
    );
  }

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ChatRequestError(
      typeof data.detail === "string"
        ? data.detail
        : `The server returned an error (${response.status}).`,
      response.status,
    );
  }

  let finished = false;
  try {
    await readSseEvents(response, (event) => {
      if (event.type === "sources") {
        const sources = (event.sources as BackendSource[]) ?? [];
        onSources(
          sources.map((source) => ({
            n: source.n,
            documentId: source.document_id,
            filename: source.filename,
            page: source.page,
            chunkId: source.chunk_id,
            snippet: source.snippet,
            similarity: source.similarity,
          })),
        );
      } else if (event.type === "delta") {
        onDelta(typeof event.content === "string" ? event.content : "");
      } else if (event.type === "done") {
        finished = true;
      } else if (event.type === "error") {
        throw new ChatRequestError(
          typeof event.message === "string"
            ? event.message
            : "The answer could not be generated.",
          typeof event.status === "number" ? event.status : undefined,
        );
      }
    });
  } catch (error) {
    if (error instanceof ChatRequestError || isAbortError(error)) throw error;
    if (error instanceof SyntaxError) {
      throw new ChatRequestError("The server sent a response that could not be read.");
    }
    throw new ChatRequestError(
      "Lost the connection to the server. The answer may be incomplete.",
    );
  }

  if (!finished) {
    throw new ChatRequestError("The answer ended unexpectedly. It may be incomplete.");
  }
}
