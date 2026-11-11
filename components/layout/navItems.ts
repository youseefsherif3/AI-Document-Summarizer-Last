import { BarChart3, FileText, LayoutDashboard, MessageSquare } from "lucide-react";

// The app's pages, shared by the sidebar links and the header title.
export const navItems = [
  { label: "Dashboard", href: "/", icon: LayoutDashboard },
  { label: "My Documents", href: "/documents", icon: FileText },
  { label: "Chat", href: "/chat", icon: MessageSquare },
  { label: "Usage", href: "/usage", icon: BarChart3 },
];

/** The sidebar label of the page at `pathname`, or the app name for any other page. */
export function pageTitle(pathname: string): string {
  return navItems.find((item) => item.href === pathname)?.label ?? "DocuMind";
}
