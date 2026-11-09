
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
} from "lucide-react";

import MainLayout from "@/components/layout/MainLayout";
import { useDocumentsContext } from "@/context/DocumentsContext";
import { ALLOWED_EXTENSIONS, MAX_CONCURRENT_UPLOADS } from "@/lib/constants";
import type { ChatMessage } from "@/types/chat";

export default function ChatPage() {
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isThinking, setIsThinking] = useState(false);
  const [sentDocumentIds, setSentDocumentIds] = useState<Set<string>>(
    new Set(),
  );

  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const { documents, error, addFiles, removeDocument, remainingSlots } =
    useDocumentsContext();

  const errorMessages = error ? error.split("\n").filter(Boolean) : [];

  // Automatically scroll to the latest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "end",
    });
  }, [messages, isThinking]);

  // Handle file selection
  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  // Send message or document summaries
  const handleSend = () => {
    const content = message.trim();

    if (isThinking) return;

    // Find completed documents whose summaries haven't been sent yet.
    const readyDocuments = documents.filter(
      (document) =>
        document.status === "completed" &&
        document.summary &&
        !sentDocumentIds.has(document.id),
    );

    // Do not send an empty message if there are no ready summaries.
    if (!content && readyDocuments.length === 0) return;

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: content
        ? content
        : `Please summarize: ${readyDocuments.map((doc) => doc.name).join(", ")}`,
      createdAt: new Date(),
    };

    setMessages((current) => [...current, userMessage]);
    setMessage("");

    // Send ready document summaries directly into the conversation.
    if (readyDocuments.length > 0) {
      const summaryContent = readyDocuments
        .map(
          (document) =>
            `Summary of ${document.name}\n\n${document.summary}`,
        )
        .join("\n\n--------------------\n\n");

      const aiMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: summaryContent,
        createdAt: new Date(),
      };

      setMessages((current) => [...current, aiMessage]);

      setSentDocumentIds((current) => {
        const updated = new Set(current);

        readyDocuments.forEach((document) => {
          updated.add(document.id);
        });

        return updated;
      });

      return;
    }

    // Keep the existing demo response for text-only messages.
    setIsThinking(true);

    setTimeout(() => {
      const aiMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        content:
          "Thanks for your message! I'm currently running in demo mode. Once the chat AI backend is connected, I'll be able to analyze your uploaded documents and answer your questions.",
        createdAt: new Date(),
      };

      setMessages((current) => [...current, aiMessage]);
      setIsThinking(false);
    }, 1200);
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
            /* Welcome Screen */
            <div className="flex min-h-0 flex-1 flex-col items-center justify-center py-8 text-center">
              <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-violet-500/10">
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
              <div className="mx-auto w-full max-w-4xl space-y-8 py-4">
                {messages.map((chatMessage) => {
                  const isUser = chatMessage.role === "user";

                  return (
                    <div
                      key={chatMessage.id}
                      className={`flex gap-3 ${
                        isUser ? "justify-end" : "justify-start"
                      }`}
                    >
                      {!isUser && (
                        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-500/10">
                          <Bot size={19} className="text-violet-400" />
                        </div>
                      )}

                      <div
                        className={`max-w-[85%] rounded-2xl px-5 py-4 sm:max-w-[75%] ${
                          isUser
                            ? "bg-violet-600 text-white"
                            : "bg-[#111827] text-gray-200"
                        }`}
                      >
                        <p className="whitespace-pre-wrap text-sm leading-7">
                          {chatMessage.content}
                        </p>
                      </div>

                      {isUser && (
                        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-500/10">
                          <UserRound size={19} className="text-blue-400" />
                        </div>
                      )}
                    </div>
                  );
                })}

                {/* Thinking Indicator */}
                {isThinking && (
                  <div className="flex items-center gap-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-500/10">
                      <Bot size={19} className="text-violet-400" />
                    </div>

                    <div className="rounded-xl bg-[#111827] px-4 py-3">
                      <LoaderCircle
                        size={19}
                        className="animate-spin text-violet-400"
                      />
                    </div>
                  </div>
                )}

                <div ref={messagesEndRef} />
              </div>
            </div>
          )}
        </div>

        {/* Uploaded Files */}
        {documents.length > 0 && (
          <div className="mt-5 flex shrink-0 flex-wrap gap-3">
            {documents.map((document) => (
              <div
                key={document.id}
                className="flex items-center gap-3 rounded-xl border border-white/10 bg-[#111827] px-3 py-2"
              >
                <FileText size={20} className="shrink-0 text-violet-400" />

                <div className="min-w-0">
                  <p className="max-w-48 truncate text-sm text-white">
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
          <div className="mt-3 shrink-0 rounded-xl border border-red-500/20 bg-red-500/5 p-3">
            <div className="mb-2 flex items-center gap-2">
              <AlertCircle size={16} className="text-red-400" />
              <h3 className="text-sm font-semibold text-red-400">
                Upload Issues
              </h3>
            </div>

            <ul className="space-y-1">
              {errorMessages.map((message, index) => (
                <li key={index} className="text-sm text-gray-300">
                  • {message}
                </li>
              ))}
            </ul>
          </div>
        )}

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
            className={`w-full resize-none bg-transparent text-sm text-white outline-none placeholder:text-gray-500 transition-all duration-300 ${
              hasStartedChat ? "p-2" : "p-3"
            }`}
          />

          <div
            className={`flex items-center justify-between border-t border-white/10 ${
              hasStartedChat ? "pt-2" : "pt-3"
            }`}
          >
            {/* Attach Files */}
            <div>
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
                className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-gray-400 transition hover:bg-white/5 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Paperclip size={18} />
                Attach files
                <span className="text-xs text-gray-600">
                  {remainingSlots > 0
                    ? `(${remainingSlots} of ${MAX_CONCURRENT_UPLOADS} slots free)`
                    : "(wait for uploads to finish)"}
                </span>
              </button>
            </div>

            {/* Send Button */}
            <button
              type="button"
              onClick={handleSend}
              disabled={!canSend || isThinking}
              className="flex items-center gap-2 rounded-xl bg-violet-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {message.trim() ? "Send" : "Send Summary"}
              <Send size={16} />
            </button>
          </div>
        </div>

        {/* Disclaimer */}
        <p className="mt-3 shrink-0 text-center text-xs text-gray-600">
          AI-generated responses may contain inaccuracies.
        </p>
      </div>
    </MainLayout>
  );
}
