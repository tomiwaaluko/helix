"use client";

import { useQuery } from "@tanstack/react-query";

import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchEval } from "@/lib/api";
import type { EvalEvent } from "@/lib/types";
import { formatTimestamp } from "@/lib/utils";

function mean(values: number[]): number {
  if (values.length === 0) return 0;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
    </div>
  );
}

export function EvalDetail({ id }: { id: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["eval", id],
    queryFn: () => fetchEval(id),
    staleTime: Infinity,
  });

  if (isError) {
    const msg = (error as Error).message;
    const is503 = msg.includes("503");
    return (
      <p className="text-sm text-muted-foreground">
        {is503
          ? "Eval details unavailable: ClickHouse is not configured."
          : `Failed to load eval: ${msg}`}
      </p>
    );
  }

  if (isLoading || !data) {
    return <Skeleton className="h-48 w-full" />;
  }

  const scorerNames = Array.from(new Set(data.map((e: EvalEvent) => e.scorer))).sort();
  const metrics = scorerNames.map((scorer) => {
    const scores = data.filter((e: EvalEvent) => e.scorer === scorer).map((e: EvalEvent) => e.score);
    return { scorer, mean: mean(scores), n: scores.length };
  });

  // Group rows by example_id for the per-example table.
  const byExample = new Map<string, EvalEvent[]>();
  for (const ev of data) {
    const bucket = byExample.get(ev.example_id) ?? [];
    bucket.push(ev);
    byExample.set(ev.example_id, bucket);
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <MetricCard label="Examples" value={String(byExample.size)} />
        {metrics.map((m) => (
          <MetricCard key={m.scorer} label={m.scorer} value={m.mean.toFixed(3)} />
        ))}
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Example</TableHead>
            <TableHead>Timestamp</TableHead>
            {scorerNames.map((s) => (
              <TableHead key={s}>{s}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from(byExample.entries()).map(([exampleId, events]) => {
            const scoreByScorer = Object.fromEntries(events.map((e) => [e.scorer, e]));
            const ts = events[0]?.timestamp;
            return (
              <TableRow key={exampleId}>
                <TableCell className="font-mono text-xs">{exampleId}</TableCell>
                <TableCell className="text-muted-foreground">
                  {ts ? formatTimestamp(ts) : "—"}
                </TableCell>
                {scorerNames.map((s) => {
                  const ev = scoreByScorer[s];
                  if (!ev) return <TableCell key={s}>—</TableCell>;
                  return (
                    <TableCell key={s}>
                      <span className={ev.passed ? "text-green-600" : "text-red-500"}>
                        {ev.score.toFixed(3)}
                      </span>
                    </TableCell>
                  );
                })}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
