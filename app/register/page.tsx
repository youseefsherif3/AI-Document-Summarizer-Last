import type { Metadata } from "next";

import AuthPage from "@/components/auth/AuthPage";

export const metadata: Metadata = { title: "Create account · DocuMind" };

export default function RegisterPage() {
  return <AuthPage mode="register" />;
}
