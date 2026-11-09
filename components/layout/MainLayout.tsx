"use client";

import { useEffect, useState, type ReactNode } from "react";
import Sidebar from "./Sidebar";
import Header from "./Header";

interface MainLayoutProps {
  children: ReactNode;
  scrollable?: boolean;
}

export default function MainLayout({ children, scrollable = true }: MainLayoutProps) {
  // Phone only: the sidebar opens as a drawer from the menu button.
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (!menuOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  return (
    <div className="flex h-dvh overflow-hidden bg-[#0B1120]">
      <Sidebar open={menuOpen} onClose={() => setMenuOpen(false)} />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Header onMenuClick={() => setMenuOpen(true)} />

        <main
          className={`min-h-0 flex-1 p-4 sm:p-6 lg:p-8 ${
            scrollable ? "overflow-y-auto custom-scrollbar" : "overflow-hidden"
          }`}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
