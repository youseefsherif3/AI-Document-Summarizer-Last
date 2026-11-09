"use client";

import type { ReactNode } from "react";
import Sidebar from "./Sidebar";
import Header from "./Header";

interface MainLayoutProps {
  children: ReactNode;
  scrollable?: boolean;
}

export default function MainLayout({ children, scrollable = true }: MainLayoutProps) {
  return (
    <div className="flex h-dvh overflow-hidden bg-[#0B1120]">
      <Sidebar />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Header />

        <main
          className={`min-h-0 flex-1 p-8 ${
            scrollable ? "overflow-y-auto custom-scrollbar" : "overflow-hidden"
          }`}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
