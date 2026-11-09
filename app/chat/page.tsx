
"use client";

import { useEffect, useRef, useState } from "react";
import {
  Bot,
  Paperclip,
  Send,
  Sparkles,
  FileText,
  X,
  UserRound,
  LoaderCircle,
  AlertCircle,
  Square,
} from "lucide-react";

import AnswerText, { citedSourceNumbers } from "@/components/chat/AnswerText";
import DocumentPicker from "@/components/chat/DocumentPicker";
import SourceList from "@/components/chat/SourceList";
import MainLayout from "@/components/layout/MainLayout";
import { useDocumentsContext } from "@/context/DocumentsContext";
import { isAbortError, streamChatAnswer } from "@/lib/chat-api";
import { ALLOWED_EXTENSIONS, MAX_CONCURRENT_UPLOADS } from "@/lib/constants";
import type { ChatMessage } from "@/types/chat";

// Earlier messages sent with each question, for follow-ups (the backend keeps the same number).
const HISTORY_MESSAGES = 6;

export default function ChatPage() {
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [sentDocumentIds, setSentDocumentIds] = useState<Set<string>>(
    new Set(),
  );
  // Empty = search all documents.
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<number[]>([]);
  const [activeSource, setActiveSource] = useState<{
    messageId: string;
    n: number;
  } | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const { documents, error, addFiles, removeDocument, remainingSlots } =
    useDocumentsContext();

  const errorMessages = error ? error.split("\n").filter(Boolean) : [];

  // Documents saved in the database can be searched.
  const searchableDocuments = documents
    .filter((document) => document.status === "completed" && document.backendId)
    .map((document) => ({ id: document.backendId as number, name: document.name }));
  const searchableIds = new Set(searchableDocuments.map((document) => document.id));
  // Drop selections whose document was deleted, so the request never names a missing one.
  const validSelectedIds = selectedDocumentIds.filter((id) => searchableIds.has(id));

  // Automatically scroll to the latest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [messages]);

  // Stop a running answer when leaving the page.
  useEffect(() => () => abortRef.current?.abort(), []);

  const updateMessage = (id: string, update: (current: ChatMessage) => ChatMessage) =>
    setMessages((current) =>
      current.map((item) => (item.id === id ? update(item) : item)),
    );

  // Ask the backend and stream the answer into a new assistant message.
  const askQuestion = async (question: string) => {
    const history = messages
      .filter((item) => item.status !== "error" && item.content.trim())
      .slice(-HISTORY_MESSAGES)
      .map((item) => ({ role: item.role, content: item.content }));

    const answerId = crypto.randomUUID();
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", content: question, createdAt: new Date() },
      {
        id: answerId,
        role: "assistant",
        content: "",
        createdAt: new Date(),
        sources: [],
        status: "streaming",
      },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    setIsStreaming(true);

    try {
      await streamChatAnswer(
        {
          message: question,
          history,
          documentIds: validSelectedIds,
          signal: controller.signal,
        },
        {
          onSources: (sources) => updateMessage(answerId, (item) => ({ ...item, sources })),
          // Append to the combined text; citations are rendered from that, not per event.
          onDelta: (text) =>
            updateMessage(answerId, (item) => ({ ...item, content: item.content + text })),
        },
      );
      updateMessage(answerId, (item) => ({ ...item, status: "done" }));
    } catch (requestError) {
      if (isAbortError(requestError)) {
        updateMessage(answerId, (item) => ({ ...item, status: "stopped" }));
      } else {
        updateMessage(answerId, (item) => ({
          ...item,
          status: "error",
          error:
            requestError instanceof Error
              ? requestError.message
              : "Something went wrong while answering.",
        }));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setIsStreaming(false);
    }
  };

  const stopAnswer = () => abortRef.current?.abort();

  // Handle file selection
  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  // Ask a typed question, or (with an empty input) post the ready document summaries.
  const handleSend = () => {
    const content = message.trim();

    if (isStreaming) return;

    if (content) {
      setMessage("");
      void askQuestion(content);
      return;
    }

    // Find completed documents whose summaries haven't been sent yet.
    const readyDocuments = documents.filter(
      (document) =>
        document.status === "completed" &&
        document.summary &&
        !sentDocumentIds.has(document.id),
    );

    if (readyDocuments.length === 0) return;

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: `Please summarize: ${readyDocuments.map((doc) => doc.name).join(", ")}`,
      createdAt: new Date(),
    };

    // Send ready document summaries directly into the conversation.
    const summaryContent = readyDocuments
      .map((document) => `Summary of ${document.name}\n\n${document.summary}`)
      .join("\n\n--------------------\n\n");

    const aiMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: summaryContent,
      createdAt: new Date(),
    };

    setMessages((current) => [...current, userMessage, aiMessage]);

    setSentDocumentIds((current) => {
      const updated = new Set(current);
      readyDocuments.forEach((document) => updated.add(document.id));
      return updated;
    });
  };

  const hasStartedChat = messages.length > 0;
  const hasUploadedFiles = documents.length > 0 || error !== null;

  const showWelcomeScreen = !hasStartedChat && !hasUploadedFiles;

  const hasReadySummaries = documents.some(
    (document) =>
      document.status === "completed" &&
      document.summary &&
      !sentDocumentIds.has(document.id),
  );

  const canSend = Boolean(message.trim()) || hasReadySummaries;

  return (
    <MainLayout scrollable={false}>
      <div className="mx-auto flex h-full min-h-0 w-full max-w-5xl flex-col">
        {/* Conversation Area */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          {showWelcomeScreen ? (
            /* Welcome Screen (center-safe: centered when it fits, scrolls from the top when not) */
            <div className="scrollbar-hidden flex min-h-0 flex-1 flex-col items-center justify-center-safe overflow-y-auto py-6 text-center sm:py-8">
              <div className="mb-5 flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl bg-violet-500/10">
                <Sparkles size={30} className="text-violet-400" />
              </div>

              <h1 className="text-2xl font-bold text-white sm:text-3xl">
                How can I help you today?
              </h1>

              <p className="mt-3 max-w-md text-sm leading-6 text-gray-400">
                Upload your documents and ask me anything about their content.
              </p>

              <div className="mt-8 grid w-full max-w-2xl grid-cols-1 gap-3 sm:grid-cols-2">
                {[
                  "Summarize my document",
                  "Extract the key points",
                  "Find important information",
                  "Explain complex concepts",
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => setMessage(suggestion)}
                    className="rounded-xl border border-white/10 bg-[#111827] p-4 text-left text-sm text-gray-300 transition hover:border-violet-500/50 hover:bg-violet-500/5"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            /* Chat Messages */
            <div className="scrollbar-hidden min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-4xl space-y-6 py-4 sm:space-y-8">
                {messages.map((chatMessage) => {
                  const isUser = chatMessage.role === "user";
                  const isAnswer = !isUser && chatMessage.sources !== undefined;
                  const sources = chatMessage.sources ?? [];
                  const answerStreaming = chatMessage.status === "streaming";
                  const activeN =
                    activeSource?.messageId === chatMessage.id ? activeSource.n : null;

                  return (
                    <div
                      key={chatMessage.id}
                      className={`flex gap-2 sm:gap-3 ${
                        isUser ? "justify-end" : "justify-start"
                      }`}
                    >
                      {!isUser && (
                        <div className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-500/10 sm:flex">
                          <Bot size={19} className="text-violet-400" />
                        </div>
                      )}

                      <div
                        className={`min-w-0 rounded-2xl px-4 py-3 sm:px-5 sm:py-4 ${
                          isAnswer ? "max-w-full sm:max-w-[75%]" : "max-w-[85%] sm:max-w-[75%]"
                        } ${
                          isUser
                            ? "bg-violet-600 text-white"
                            : "bg-[#111827] text-gray-200"
                        }`}
                      >
                        {isAnswer ? (
                          <>
                            {chatMessage.content ? (
                              <AnswerText
                                text={chatMessage.content}
                                sources={sources}
                                activeSource={activeN}
                                onCite={(n) =>
                                  setActiveSource({ messageId: chatMessage.id, n })
                                }
                              />
                            ) : answerStreaming ? (
                              <p className="flex items-center gap-2 text-sm text-gray-400">
                                <LoaderCircle
                                  size={16}
                                  className="animate-spin text-violet-400"
                                />
                                Searching your documents…
                              </p>
                            ) : null}

                            {chatMessage.status === "stopped" && (
                              <p className="mt-2 text-xs text-gray-500">
                                {chatMessage.content
                                  ? "Stopped. The answer is incomplete."
                                  : "Stopped before an answer arrived."}
                              </p>
                            )}

                            {chatMessage.status === "error" && (
                              <div className="mt-2 flex items-start gap-2 rounded-xl border border-red-500/20 bg-red-500/5 px-3 py-2">
                                <AlertCircle
                                  size={16}
                                  className="mt-0.5 shrink-0 text-red-400"
                                />
                                <p className="min-w-0 text-sm text-red-300 [overflow-wrap:anywhere]">
                                  {chatMessage.error}
                                </p>
                              </div>
                            )}

                            <SourceList
                              messageId={chatMessage.id}
                              sources={sources}
                              citedNumbers={citedSourceNumbers(
                                chatMessage.content,
                                sources,
                              )}
                              activeSource={activeN}
                              onSelect={(n) =>
                                setActiveSource({ messageId: chatMessage.id, n })
                              }
                              isStreaming={answerStreaming}
                            />
                          </>
                        ) : (
                          <p className="whitespace-pre-wrap text-sm leading-7 [overflow-wrap:anywhere]">
                            {chatMessage.content}
                          </p>
                        )}
                      </div>

                      {isUser && (
                        <div className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-500/10 sm:flex">
                          <UserRound size={19} className="text-blue-400" />
                        </div>
                      )}
                    </div>
                  );
                })}

                <div ref={messagesEndRef} />
              </div>
            </div>
          )}
        </div>

        {/* Uploaded Files (one sideways-scrolling row below lg, so messages keep their height) */}
        {documents.length > 0 && (
          <div className="mt-3 flex shrink-0 gap-2 overflow-x-auto pb-1 sm:mt-5 sm:gap-3 lg:max-h-36 lg:flex-wrap lg:overflow-y-auto lg:pb-0">
            {documents.map((document) => (
              <div
                key={document.id}
                className="flex shrink-0 items-center gap-3 rounded-xl border border-white/10 bg-[#111827] px-3 py-2"
              >
                <FileText size={20} className="shrink-0 text-violet-400" />

                <div className="min-w-0">
                  <p className="max-w-36 truncate text-sm text-white sm:max-w-48">
                    {document.name}
                  </p>

                  <p className="text-xs text-gray-500">
                    {document.status === "completed"
                      ? "Ready to send"
                      : document.status === "failed"
                        ? "Processing failed"
                        : "Processing..."}
                    {" · "}
                    {(document.size / 1024 / 1024).toFixed(2)} MB
                  </p>
                </div>

                <button
                  type="button"
                  onClick={() => removeDocument(document.id)}
                  aria-label={`Remove ${document.name}`}
                  className="rounded-lg p-1 text-gray-500 transition hover:bg-white/10 hover:text-red-400"
                >
                  <X size={16} />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Upload Error Messages */}
        {errorMessages.length > 0 && (
          <div className="mt-3 max-h-28 shrink-0 overflow-y-auto rounded-xl border border-red-500/20 bg-red-500/5 p-3">
            <div className="mb-2 flex items-center gap-2">
              <AlertCircle size={16} className="text-red-400" />
              <h3 className="text-sm font-semibold text-red-400">
                Upload Issues
              </h3>
            </div>

            <ul className="space-y-1">
              {errorMessages.map((message, index) => (
                <li key={index} className="text-sm text-gray-300 [overflow-wrap:anywhere]">
                  • {message}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Which documents to search */}
        <DocumentPicker
          documents={searchableDocuments}
          selectedIds={validSelectedIds}
          onChange={setSelectedDocumentIds}
          disabled={isStreaming}
        />

        {/* Message Input */}
        <div
          className={`shrink-0 rounded-2xl border border-white/10 bg-[#111827] transition-all duration-300 ${
            hasStartedChat ? "mt-3 p-2" : "mt-4 p-3"
          }`}
        >
          <textarea
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                handleSend();
              }
            }}
            placeholder="Ask anything about your documents..."
            rows={hasStartedChat ? 1 : 2}
            className={`w-full resize-none bg-transparent text-base text-white outline-none sm:text-sm placeholder:text-gray-500 transition-all duration-300 ${
              hasStartedChat ? "p-2" : "p-3"
            }`}
          />

          <div
            className={`flex items-center justify-between gap-2 border-t border-white/10 ${
              hasStartedChat ? "pt-2" : "pt-3"
            }`}
          >
            {/* Attach Files */}
            <div className="min-w-0">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept={ALLOWED_EXTENSIONS.join(",")}
                className="hidden"
                onChange={handleFileChange}
              />

              <button
                type="button"
                disabled={remainingSlots <= 0}
                onClick={() => fileInputRef.current?.click()}
                className="flex items-center gap-2 rounded-lg px-2 py-2 text-sm text-gray-400 sm:px-3 transition hover:bg-white/5 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Paperclip size={18} className="shrink-0" />
                <span className="whitespace-nowrap">Attach files</span>
                {/* Shorter on phones so Send still fits on the same row */}
                <span className="whitespace-nowrap text-xs text-gray-600 sm:hidden">
                  {remainingSlots > 0
                    ? `(${remainingSlots}/${MAX_CONCURRENT_UPLOADS} free)`
                    : "(busy)"}
                </span>
                <span className="hidden text-xs text-gray-600 sm:inline">
                  {remainingSlots > 0
                    ? `(${remainingSlots} of ${MAX_CONCURRENT_UPLOADS} slots free)`
                    : "(wait for uploads to finish)"}
                </span>
              </button>
            </div>

            {/* Send / Stop Button */}
            {isStreaming ? (
              <button
                type="button"
                onClick={stopAnswer}
                className="flex shrink-0 items-center gap-2 rounded-xl border border-white/15 bg-white/5 px-4 py-2 text-sm font-medium text-gray-200 transition hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-300"
              >
                Stop
                <Square size={14} className="fill-current" />
              </button>
            ) : (
              <button
                type="button"
                onClick={handleSend}
                disabled={!canSend}
                className="flex shrink-0 items-center gap-2 whitespace-nowrap rounded-xl bg-violet-600 px-3 py-2 sm:px-4 text-sm font-medium text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {message.trim() ? "Send" : "Send Summary"}
                <Send size={16} />
              </button>
            )}
          </div>
        </div>

        {/* Disclaimer */}
        <p className="mt-2 shrink-0 text-center text-xs text-gray-600 sm:mt-3">
          AI-generated responses may contain inaccuracies.
        </p>
      </div>
    </MainLayout>
  );
}
