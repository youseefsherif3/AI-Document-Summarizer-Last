import { Suspense } from "react";
import { Sparkles } from "lucide-react";

import AuthForm from "./AuthForm";

/** The sign-in and register pages: the form on a card, without the app's sidebar. */
export default function AuthPage({ mode }: { mode: "login" | "register" }) {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-[#0B1120] px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-8 flex items-center justify-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-violet-600 text-white">
            <Sparkles size={22} />
          </div>
          <div>
            <p className="text-lg font-bold text-white">DocuMind</p>
            <p className="text-xs text-gray-500">AI Document Summarizer</p>
          </div>
        </div>

        <div className="rounded-2xl border border-white/10 bg-[#111827] p-6 shadow-xl sm:p-8">
          {/* The form reads ?next= from the URL, so it renders in the browser. */}
          <Suspense fallback={<div className="h-80" aria-busy="true" />}>
            <AuthForm mode={mode} />
          </Suspense>
        </div>
      </div>
    </div>
  );
}
