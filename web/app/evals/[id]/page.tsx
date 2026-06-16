import Link from "next/link";

import { EvalDetail } from "@/components/eval-detail";

export default function EvalPage({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Link href="/evals" className="hover:text-foreground">
          Evals
        </Link>
        <span>/</span>
        <span className="font-mono">{params.id}</span>
      </div>
      <h1 className="text-xl font-semibold">Eval: {params.id}</h1>
      <EvalDetail id={params.id} />
    </div>
  );
}
