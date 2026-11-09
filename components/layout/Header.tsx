"use client";

import { usePathname } from "next/navigation";
import { Bell, Menu, Search, UserRound } from "lucide-react";

import { pageTitle } from "./navItems";

interface HeaderProps {
  // Opens the sidebar drawer on phones.
  onMenuClick: () => void;
}

export default function Header({ onMenuClick }: HeaderProps) {
  const pathname = usePathname();

  return (
    <header className="flex h-16 shrink-0 items-center justify-between gap-3 border-b border-white/10 bg-[#111827] px-4 sm:h-20 sm:gap-5 sm:px-6 lg:px-8">
      <div className="flex min-w-0 items-center gap-2">
        <button
          type="button"
          onClick={onMenuClick}
          aria-label="Open menu"
          className="-ml-2 shrink-0 rounded-xl p-2 text-gray-400 transition hover:bg-white/5 hover:text-white md:hidden"
        >
          <Menu size={20} />
        </button>

        <div className="min-w-0">
          <h2 className="truncate text-lg font-semibold text-white">{pageTitle(pathname)}</h2>
          <p className="hidden truncate text-sm text-gray-500 sm:block">
            Manage and summarize your documents
          </p>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2 sm:gap-5">
        <div className="hidden items-center gap-2 rounded-xl border border-white/10 bg-[#1F2937] px-3 py-2 sm:flex">
          <Search size={17} className="text-gray-500" />
          <input
            type="text"
            placeholder="Search documents..."
            className="w-32 bg-transparent text-sm text-white outline-none placeholder:text-gray-500 lg:w-44"
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
