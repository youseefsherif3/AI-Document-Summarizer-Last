/**
 * The FastAPI backend's URL, for server-side code only (route handlers). The browser never
 * uses it: it calls this app's /api, which next.config.ts rewrites to the same URL.
 */
export function backendUrl(): string {
  const url = process.env.BACKEND_URL;
  if (!url && process.env.VERCEL) {
    throw new Error("BACKEND_URL is not set for this Vercel environment.");
  }
  return (url || "http://127.0.0.1:8000").replace(/\/+$/, "");
}
