"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  FileText,
  MessageSquare,
  Settings,
  HelpCircle,
  Sparkles,
  X,
} from "lucide-react";

const navItems = [
  { label: "Dashboard", href: "/", icon: LayoutDashboard },
  { label: "My Documents", href: "/documents", icon: FileText },
  { label: "Chat", href: "/chat", icon: MessageSquare },
];

interface SidebarProps {
  // Phone only: whether the drawer is open. From md up the sidebar is always shown.
  open: boolean;
  onClose: () => void;
}

export default function Sidebar({ open, onClose }: SidebarProps) {
  const pathname = usePathname();

  return (
    <>
      {open && (
        <div
          aria-hidden="true"
          onClick={onClose}
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-50 flex h-full w-64 shrink-0 flex-col overflow-y-auto border-r border-white/10 bg-[#111827] px-5 py-6 text-gray-300 transition-[translate,visibility] duration-200 md:visible md:static md:translate-x-0 ${
          open ? "visible translate-x-0" : "invisible -translate-x-full"
        }`}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close menu"
          className="absolute right-3 top-3 rounded-lg p-2 text-gray-400 transition hover:bg-white/5 hover:text-white md:hidden"
        >
          <X size={18} />
        </button>

        <Link href="/" onClick={onClose} className="mb-10 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-violet-600 text-white">
            <Sparkles size={21} />
          </div>
          <div>
            <h1 className="text-lg font-bold text-white">DocuMind</h1>
            <p className="text-xs text-gray-500">AI Document Summarizer</p>
          </div>
        </Link>

        <p className="mb-3 px-3 text-xs font-semibold uppercase tracking-wider text-gray-500">
          Workspace
        </p>

        <nav className="space-y-2">
          {navItems.map(({ label, href, icon: Icon }) => {
            const active = pathname === href;

            return (
              <Link
                key={href}
                href={href}
                onClick={onClose}
                className={`flex items-center gap-3 rounded-xl px-3 py-3 text-sm transition ${
                  active
                    ? "bg-violet-600/15 font-medium text-violet-400"
                    : "text-gray-400 hover:bg-white/5 hover:text-white"
                }`}
              >
                <Icon size={19} />
                {label}
              </Link>
            );
          })}
        </nav>

        <div className="mt-auto space-y-2">
          <Link
            href="/settings"
            onClick={onClose}
            className="flex items-center gap-3 rounded-xl px-3 py-3 text-sm text-gray-400 transition hover:bg-white/5 hover:text-white"
          >
            <Settings size={19} />
            Settings
          </Link>

          <Link
            href="/help"
            onClick={onClose}
            className="flex items-center gap-3 rounded-xl px-3 py-3 text-sm text-gray-400 transition hover:bg-white/5 hover:text-white"
          >
            <HelpCircle size={19} />
            Help & Support
          </Link>

          <div className="mt-5 border-t border-white/10 pt-5">
            <p className="text-xs text-gray-500">Powered by AI</p>
            <p className="mt-1 text-sm font-medium text-white">
              Your smarter workspace
            </p>
          </div>
        </div>
      </aside>
    </>
  );
}
