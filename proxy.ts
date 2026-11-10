import { NextResponse, type NextRequest } from "next/server";

// The backend's session cookie (backend/app/auth.py). This only checks that a cookie is
// there, for fast routing; whether the session is valid is decided by the backend, and a
// 401 from it sends the user to the sign-in page (context/AuthContext.tsx).
const SESSION_COOKIE = "documind_session";
const PUBLIC_PAGES = ["/login", "/register"];

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const hasSession = request.cookies.has(SESSION_COOKIE);
  const isPublic = PUBLIC_PAGES.includes(pathname);

  if (!hasSession && !isPublic) {
    const login = new URL("/login", request.url);
    login.searchParams.set("next", pathname + search);
    return NextResponse.redirect(login);
  }
  if (hasSession && isPublic) {
    return NextResponse.redirect(new URL("/", request.url));
  }
  return NextResponse.next();
}

export const config = {
  // Pages only. Not /api (the backend checks the session itself, and the proxy would
  // buffer upload bodies), not Next's own files, and not files with an extension.
  matcher: ["/((?!api/|_next/|.*\\..*).*)"],
};
