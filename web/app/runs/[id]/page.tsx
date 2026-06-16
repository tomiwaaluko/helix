import Link from "next/link";

import { RunDetail } from "@/components/run-detail";

export default function RunDetailPage({ params }: { params: { id: string } }) {
  return (
    <div className="space-y-4">
      <Link href="/runs" className="text-sm text-muted-foreground hover:text-foreground">
        ← Runs
      </Link>
      <RunDetail id={params.id} />
    </div>
  );
}
