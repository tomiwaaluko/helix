import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { Providers } from "@/lib/query-client";
import "./globals.css";

export const metadata: Metadata = {
  title: "Helix",
  description: "Helix runtime dashboard",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <Providers>
          <header className="border-b">
            <div className="mx-auto flex max-w-6xl items-center gap-4 px-6 py-3">
              <Link href="/runs" className="font-semibold">
                Helix
              </Link>
              <nav className="flex gap-4 text-sm text-muted-foreground">
                <Link href="/runs" className="hover:text-foreground">
                  Runs
                </Link>
                <Link href="/evals" className="hover:text-foreground">
                  Evals
                </Link>
              </nav>
            </div>
          </header>
          <main className="mx-auto max-w-6xl px-6 py-6">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
