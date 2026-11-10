"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

import { apiUrl, loadPage, SESSION_ENDED_EVENT } from "@/lib/api";

export interface User {
  id: number;
  email: string;
}

interface AuthContextType {
  // null while loading and when signed out
  user: User | null;
  loading: boolean;
  logout: () => Promise<void>;
}

// Pages that work without a session (proxy.ts uses the same list).
export const PUBLIC_PAGES = ["/login", "/register"];

const AuthContext = createContext<AuthContextType | undefined>(undefined);

let leaving = false;

/**
 * The session ended: clear the (now useless) cookie, then go to the sign-in page and come
 * back here afterwards. Clearing the cookie matters: proxy.ts sends visitors that still
 * have a cookie away from /login, which would otherwise loop.
 */
async function sendToSignIn() {
  if (leaving || PUBLIC_PAGES.includes(window.location.pathname)) return;
  leaving = true;
  await fetch(apiUrl("/auth/logout"), { method: "POST" }).catch(() => undefined);
  const next = window.location.pathname + window.location.search;
  loadPage(`/login?next=${encodeURIComponent(next)}`);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Who is signed in, once per page load.
  useEffect(() => {
    let active = true;
    void fetch(apiUrl("/auth/me"), { cache: "no-store" })
      .then(async (response) => {
        if (!active) return;
        if (response.ok) {
          setUser((await response.json()) as User);
        } else if (response.status === 401) {
          void sendToSignIn();
        }
        setLoading(false);
      })
      .catch(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  // Any backend call that gets a 401 (see apiFetch) ends up here.
  useEffect(() => {
    const onSessionEnded = () => void sendToSignIn();
    window.addEventListener(SESSION_ENDED_EVENT, onSessionEnded);
    return () => window.removeEventListener(SESSION_ENDED_EVENT, onSessionEnded);
  }, []);

  const logout = useCallback(async () => {
    leaving = true;
    await fetch(apiUrl("/auth/logout"), { method: "POST" }).catch(() => undefined);
    // A full page load, so nothing of this user's data stays in memory.
    loadPage("/login");
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, logout }}>{children}</AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
