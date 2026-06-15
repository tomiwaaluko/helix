"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchEvals } from "@/lib/api";
import { formatTimestamp } from "@/lib/utils";

export function EvalTable() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["evals"],
    queryFn: fetchEvals,
    refetchInterval: 10_000,
  });

  if (isError) {
    const msg = (error as Error).message;
    const is503 = msg.includes("503");
    return (
      <p className="text-sm text-muted-foreground">
        {is503
          ? "Eval results unavailable: ClickHouse is not configured."
          : `Failed to load evals: ${msg}`}
      </p>
    );
  }

  // Collect all scorer names across all evals for column headers.
  const scorerNames = Array.from(
    new Set((data ?? []).flatMap((e) => e.scorers.map((s) => s.scorer))),
  ).sort();

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Eval ID</TableHead>
          <TableHead>Examples</TableHead>
          <TableHead>Finished at</TableHead>
          {scorerNames.map((s) => (
            <TableHead key={s}>{s}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {isLoading ? (
          <TableRow>
            <TableCell colSpan={3 + scorerNames.length}>
              <Skeleton className="h-6 w-full" />
            </TableCell>
          </TableRow>
        ) : data && data.length > 0 ? (
          data.map((ev) => {
            const meanByScorer = Object.fromEntries(ev.scorers.map((s) => [s.scorer, s.mean]));
            return (
              <TableRow key={ev.eval_id}>
                <TableCell>
                  <Link
                    href={`/evals/${ev.eval_id}`}
                    className="font-mono text-sm hover:underline"
                  >
                    {ev.eval_id}
                  </Link>
                </TableCell>
                <TableCell>{ev.examples}</TableCell>
                <TableCell className="text-muted-foreground">
                  {formatTimestamp(ev.finished_at)}
                </TableCell>
                {scorerNames.map((s) => (
                  <TableCell key={s}>
                    {meanByScorer[s] !== undefined ? meanByScorer[s].toFixed(3) : "—"}
                  </TableCell>
                ))}
              </TableRow>
            );
          })
        ) : (
          <TableRow>
            <TableCell colSpan={3 + scorerNames.length} className="text-center text-muted-foreground">
              No eval runs yet.
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
