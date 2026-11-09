"use client";

import { Bell, Search, UserRound } from "lucide-react";

export default function Header() {
  return (
    <header className="flex h-20 items-center justify-between border-b border-white/10 bg-[#111827] px-8">
      <div>
        <h2 className="text-lg font-semibold text-white">Dashboard</h2>
        <p className="text-sm text-gray-500">
          Manage and summarize your documents
        </p>
      </div>

      <div className="flex items-center gap-5">
        <div className="flex items-center gap-2 rounded-xl border border-white/10 bg-[#1F2937] px-3 py-2">
          <Search size={17} className="text-gray-500" />
          <input
            type="text"
            placeholder="Search documents..."
            className="w-44 bg-transparent text-sm text-white outline-none placeholder:text-gray-500"
          />
        </div>

        <button
          type="button"
          aria-label="Notifications"
          className="relative rounded-xl p-2 text-gray-400 transition hover:bg-white/5 hover:text-white"
        >
          <Bell size={20} />
          <span className="absolute right-2 top-2 h-2 w-2 rounded-full bg-violet-500" />
        </button>

        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-violet-600/20 text-violet-400">
          <UserRound size={20} />
        </div>
      </div>
    </header>
  );
}
