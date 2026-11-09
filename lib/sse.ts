/** One server-sent event whose `data:` line is a JSON object with a `type`. */
export interface SseEvent {
  type: string;
  [key: string]: unknown;
}

/**
 * Read a `text/event-stream` response and call `onEvent` for each event, in order.
 * Events can arrive split across network chunks, so text is buffered until a blank line.
 * Throws if the stream breaks or an event is not valid JSON.
 */
export async function readSseEvents(
  response: Response,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  if (!response.body) {
    throw new Error("The response has no body to stream.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");

      const frames = buffer.split("\n\n");
      buffer = done ? "" : (frames.pop() ?? "");

      for (const frame of frames) {
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).replace(/^ /, ""))
          .join("\n");
        if (data) {
          onEvent(JSON.parse(data) as SseEvent);
        }
      }

      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}
