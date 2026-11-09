import { Sparkles } from "lucide-react";

export default function WelcomeSection() {
  return (
    <section className="mb-6 sm:mb-8">
      <div className="flex items-center gap-2 text-sm text-violet-400">
        <Sparkles size={16} />
        <span>AI-powered workspace</span>
      </div>

      <h1 className="mt-3 text-2xl font-bold tracking-tight text-white sm:text-3xl">
        Welcome back!
      </h1>

      <p className="mt-2 text-gray-400">
        Upload your documents and let AI turn them into clear, useful summaries.
      </p>
    </section>
  );
}
