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
import { fetchLlmCalls } from "@/lib/api";
import type { LlmCallRow } from "@/lib/types";
import { formatTimestamp } from "@/lib/utils";

export function LlmCallTable({ runId }: { runId?: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["llm-calls", runId ?? ""],
    queryFn: () => fetchLlmCalls(runId),
    staleTime: Infinity,
  });

  if (isError) {
    const msg = (error as Error).message;
    const is503 = msg.includes("503");
    return (
      <p className="text-sm text-muted-foreground">
        {is503
          ? "LLM call data unavailable: ClickHouse is not configured."
          : `Failed to load LLM calls: ${msg}`}
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Model</TableHead>
          <TableHead>Provider</TableHead>
          <TableHead>Prompt</TableHead>
          <TableHead>Completion</TableHead>
          <TableHead>Total</TableHead>
          <TableHead>Cost (USD)</TableHead>
          <TableHead>Run ID</TableHead>
          <TableHead>Start time</TableHead>
          <TableHead>ms</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {isLoading ? (
          <TableRow>
            <TableCell colSpan={9}>
              <Skeleton className="h-6 w-full" />
            </TableCell>
          </TableRow>
        ) : data && data.length > 0 ? (
          data.map((row: LlmCallRow) => (
            <TableRow key={row.span_id}>
              <TableCell className="font-mono text-xs">{row.model}</TableCell>
              <TableCell className="text-xs">{row.provider}</TableCell>
              <TableCell className="tabular-nums">{row.prompt_tokens}</TableCell>
              <TableCell className="tabular-nums">{row.completion_tokens}</TableCell>
              <TableCell className="tabular-nums">{row.total_tokens}</TableCell>
              <TableCell className="tabular-nums text-xs">{row.cost_usd.toFixed(5)}</TableCell>
              <TableCell className="font-mono text-xs">{row.run_id || "—"}</TableCell>
              <TableCell className="text-muted-foreground">
                {formatTimestamp(row.start_time)}
              </TableCell>
              <TableCell>{row.duration_ms}</TableCell>
            </TableRow>
          ))
        ) : (
          <TableRow>
            <TableCell colSpan={9} className="text-center text-muted-foreground">
              No LLM call data yet.
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
