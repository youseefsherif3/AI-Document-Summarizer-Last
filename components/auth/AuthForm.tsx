"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { AlertCircle, LoaderCircle } from "lucide-react";

import { apiUrl, loadPage, safeNextPath } from "@/lib/api";

const PASSWORD_MIN = 10;
const PASSWORD_MAX = 128;

type Mode = "login" | "register";

const TEXT: Record<Mode, { title: string; subtitle: string; submit: string; busy: string }> = {
  login: {
    title: "Sign in to DocuMind",
    subtitle: "Summarize and chat with your documents.",
    submit: "Sign in",
    busy: "Signing in…",
  },
  register: {
    title: "Create your account",
    subtitle: "Your documents stay private to your account.",
    submit: "Create account",
    busy: "Creating account…",
  },
};

const inputClass =
  "w-full rounded-xl border border-white/10 bg-[#1F2937] px-4 py-3 text-sm text-white " +
  "outline-none transition placeholder:text-gray-500 focus:border-violet-500 " +
  "focus:ring-2 focus:ring-violet-500/30";

export default function AuthForm({ mode }: { mode: Mode }) {
  const searchParams = useSearchParams();
  const next = safeNextPath(searchParams.get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const text = TEXT[mode];

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    if (mode === "register") {
      if (password.length < PASSWORD_MIN || password.length > PASSWORD_MAX) {
        setError(`The password must be ${PASSWORD_MIN} to ${PASSWORD_MAX} characters long.`);
        return;
      }
      if (password !== confirm) {
        setError("The passwords don't match.");
        return;
      }
    }

    setSubmitting(true);
    try {
      const response = await fetch(apiUrl(`/auth/${mode}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (response.ok) {
        // A full page load, so the whole app starts with the new session.
        loadPage(next);
        return;
      }
      const data = await response.json().catch(() => ({}));
      setError(
        typeof data.detail === "string"
          ? data.detail
          : "Something went wrong. Please try again.",
      );
    } catch {
      setError("Could not reach the server. Please try again.");
    }
    setSubmitting(false);
  };

  const otherPage = mode === "login" ? "/register" : "/login";
  const otherHref = next === "/" ? otherPage : `${otherPage}?next=${encodeURIComponent(next)}`;

  return (
    <>
      <h1 className="text-2xl font-semibold text-white">{text.title}</h1>
      <p className="mt-2 text-sm text-gray-400">{text.subtitle}</p>

      <form onSubmit={onSubmit} className="mt-8 space-y-4" noValidate>
        <div>
          <label htmlFor="email" className="mb-2 block text-sm font-medium text-gray-300">
            Email
          </label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className={inputClass}
            placeholder="you@example.com"
          />
        </div>

        <div>
          <label htmlFor="password" className="mb-2 block text-sm font-medium text-gray-300">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className={inputClass}
          />
          {mode === "register" && (
            <p className="mt-2 text-xs text-gray-500">
              {PASSWORD_MIN} to {PASSWORD_MAX} characters.
            </p>
          )}
        </div>

        {mode === "register" && (
          <div>
            <label htmlFor="confirm" className="mb-2 block text-sm font-medium text-gray-300">
              Confirm password
            </label>
            <input
              id="confirm"
              type="password"
              autoComplete="new-password"
              required
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              className={inputClass}
            />
          </div>
        )}

        {error && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300"
          >
            <AlertCircle size={18} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-violet-600 px-4 py-3 text-sm font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting && <LoaderCircle size={18} className="animate-spin" />}
          {submitting ? text.busy : text.submit}
        </button>
      </form>

      <p className="mt-6 text-center text-sm text-gray-400">
        {mode === "login" ? "No account yet? " : "Already have an account? "}
        <Link href={otherHref} className="font-medium text-violet-400 hover:text-violet-300">
          {mode === "login" ? "Create one" : "Sign in"}
        </Link>
      </p>
    </>
  );
}
