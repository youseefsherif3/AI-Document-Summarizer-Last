import { handleUpload, type HandleUploadBody } from "@vercel/blob/client";
import { NextResponse } from "next/server";

const allowedContentTypes = [
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain",
];

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as HandleUploadBody;
    const result = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname) => {
        const origin = request.headers.get("origin");
        const requestHost = request.headers.get("host");

        if (!origin || !requestHost || new URL(origin).host !== requestHost) {
          throw new Error("Uploads must come from this application.");
        }

        if (!/^documind\/uploads\/[A-Za-z0-9._-]+\.(pdf|docx|txt)$/i.test(pathname)) {
          throw new Error("Invalid upload path.");
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
