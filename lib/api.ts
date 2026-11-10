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
