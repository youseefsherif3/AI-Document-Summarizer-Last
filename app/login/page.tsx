import type { Metadata } from "next";

import AuthPage from "@/components/auth/AuthPage";

export const metadata: Metadata = { title: "Sign in · DocuMind" };

export default function LoginPage() {
  return <AuthPage mode="login" />;
}
