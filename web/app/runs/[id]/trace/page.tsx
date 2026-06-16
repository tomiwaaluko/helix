import Link from "next/link";
import { SpanTree } from "@/components/span-tree";

// Trace view for a run — server shell, client span tree.
export default async function TracePage({ params }: { params: { id: string } }) {
  const { id } = params;
  return (
    <main className="container mx-auto px-4 py-8 max-w-5xl">
      <nav className="text-sm text-muted-foreground mb-6 flex items-center gap-1.5">
        <Link href="/runs" className="hover:underline">
          Runs
        </Link>
        <span>/</span>
        <Link href={`/runs/${id}`} className="hover:underline font-mono">
          {id}
        </Link>
        <span>/</span>
        <span className="text-foreground">Trace</span>
      </nav>

      <h1 className="text-2xl font-semibold mb-6">Trace</h1>
      <SpanTree runId={id} />
    </main>
  );
}
