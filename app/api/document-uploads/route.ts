import { handleUpload, type HandleUploadBody } from "@vercel/blob/client";
import { NextResponse } from "next/server";

import { backendUrl } from "@/lib/backend-url";
import { isUserUploadPath } from "@/lib/upload-paths";

const allowedContentTypes = [
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain",
];

class NotSignedInError extends Error {}

/** The signed-in user's ID, checked by the backend with the request's session cookie. */
async function signedInUserId(request: Request): Promise<number> {
  const response = await fetch(`${backendUrl()}/auth/me`, {
    headers: { cookie: request.headers.get("cookie") ?? "" },
    cache: "no-store",
  });
  if (response.status === 401) {
    throw new NotSignedInError("Not signed in.");
  }
  if (!response.ok) {
    throw new Error(`Could not check the session (backend answered ${response.status}).`);
  }
  const user = (await response.json()) as { id: number };
  return user.id;
}

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as HandleUploadBody;
    const result = await handleUpload({
      body,
      request,
      // Only for the browser's token request. Vercel's "upload completed" callback is
      // verified by handleUpload itself and needs no session.
      onBeforeGenerateToken: async (pathname) => {
        const origin = request.headers.get("origin");
        const requestHost = request.headers.get("host");

        if (!origin || !requestHost || new URL(origin).host !== requestHost) {
          throw new Error("Uploads must come from this application.");
        }

        // The user ID comes from the session, never from the browser's request.
        const userId = await signedInUserId(request);
        if (!isUserUploadPath(pathname, userId)) {
          throw new Error("Invalid upload path: not in the signed-in user's folder.");
        }

        return {
          access: "private",
          allowedContentTypes,
          maximumSizeInBytes: 10 * 1024 * 1024,
          addRandomSuffix: true,
          validUntil: Date.now() + 5 * 60 * 1000,
        };
      },
      onUploadCompleted: async () => undefined,
    });

    return NextResponse.json(result);
  } catch (error) {
    if (error instanceof NotSignedInError) {
      return NextResponse.json({ error: "Not signed in." }, { status: 401 });
    }
    // The response stays generic; the reason goes to the server logs only.
    console.error(
      "Blob upload authorization failed:",
      error instanceof Error ? error.message : error,
    );
    return NextResponse.json(
      { error: "Could not authorize this document upload." },
      { status: 400 },
    );
  }
}
