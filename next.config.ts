import type { NextConfig } from "next";

import { backendUrl } from "./lib/backend-url";

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
    // backendUrl() fails the deployment if BACKEND_URL is missing on Vercel, instead of
    // silently forwarding /api to localhost.
    return [{ source: "/api/:path*", destination: `${backendUrl()}/:path*` }];
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
