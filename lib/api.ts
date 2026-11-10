/**
 * Backend URLs for the browser.
 *
 * Every backend call goes through this app's own `/api/*` path, which next.config.ts
 * rewrites to the FastAPI backend (BACKEND_URL). The browser never calls the backend's
 * domain directly, so the backend's cookies are first-party and no CORS is involved.
 */
export const API_BASE = "/api";

/** `apiUrl("/documents")` → `"/api/documents"`. */
export function apiUrl(path: string): string {
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

/** Fired when a backend call says the session has ended (401); AuthContext handles it. */
export const SESSION_ENDED_EVENT = "documind:session-ended";

/**
 * `fetch` for backend calls that need a session. A 401 means the session ended (expired,
 * revoked, or signed out in another tab), so the user is sent to the sign-in page.
 * Not for the sign-in calls themselves, where a 401 just means a wrong password.
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(apiUrl(path), init);
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event(SESSION_ENDED_EVENT));
  }
  return response;
}

/**
 * Go to a page with a full page load, not `router.push()` (which Next's lint rule
 * suggests). On sign-in and sign-out this is deliberate: the root layout's providers keep
 * the previous account's documents in memory across client-side navigation, and a full
 * load starts the next account with nothing left over.
 */
export function loadPage(path: string): void {
  window.location.assign(path);
}

/**
 * Where to go after signing in: the `?next=` path if it is a path on this site, else "/".
 * Anything else ("https://…", "//evil.example", "/\\evil") could send the user to
 * another site, so it is ignored.
 */
export function safeNextPath(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) {
    return "/";
  }
  return next;
}
