/**
 * Where temporary Blob uploads go: each user has their own folder,
 * `documind/uploads/{userId}/`. The upload-token route only signs paths in the signed-in
 * user's folder, and the backend only reads blobs from that same folder, so one user can
 * never make the backend ingest (or delete) another user's upload.
 */
export const UPLOAD_ROOT = "documind/uploads/";

export function userUploadPrefix(userId: number): string {
  return `${UPLOAD_ROOT}${userId}/`;
}

/** A file directly in the user's folder, with an allowed extension. */
export function isUserUploadPath(pathname: string, userId: number): boolean {
  const prefix = userUploadPrefix(userId);
  if (!pathname.startsWith(prefix)) return false;
  return /^[A-Za-z0-9._-]+\.(pdf|docx|txt)$/i.test(pathname.slice(prefix.length));
}
