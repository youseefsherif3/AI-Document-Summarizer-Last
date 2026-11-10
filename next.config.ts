import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  distDir: process.env.NEXT_BUILD_DIR || ".next",
  experimental: {
    agentFeedback: true,
  },
  // The browser only talks to this app's domain; /api/* is forwarded to the FastAPI backend
  // (BACKEND_URL, read at build time), so the backend's session cookie is first-party.
  // A plain array means "afterFiles": routes under app/api/ (document-uploads) win first.
  async rewrites() {
    if (!process.env.BACKEND_URL && process.env.VERCEL) {
      // Fail the deployment instead of silently forwarding /api to localhost.
      throw new Error("BACKEND_URL is not set for this Vercel environment.");
    }
    const backend = (process.env.BACKEND_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
    return [{ source: "/api/:path*", destination: `${backend}/:path*` }];
  },
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
