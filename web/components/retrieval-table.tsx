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
import { fetchRetrievals } from "@/lib/api";
import type { RetrievalRow } from "@/lib/types";
import { formatTimestamp } from "@/lib/utils";

export function RetrievalTable({ runId }: { runId?: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["retrievals", runId ?? ""],
    queryFn: () => fetchRetrievals(runId),
    staleTime: Infinity,
  });

  if (isError) {
    const msg = (error as Error).message;
    const is503 = msg.includes("503");
    return (
      <p className="text-sm text-muted-foreground">
        {is503
          ? "Retrieval data unavailable: ClickHouse is not configured."
          : `Failed to load retrievals: ${msg}`}
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Query</TableHead>
          <TableHead>Retriever</TableHead>
          <TableHead>Top-k</TableHead>
          <TableHead>Recall@k</TableHead>
          <TableHead>Run ID</TableHead>
          <TableHead>Start time</TableHead>
          <TableHead>ms</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {isLoading ? (
          <TableRow>
            <TableCell colSpan={7}>
              <Skeleton className="h-6 w-full" />
            </TableCell>
          </TableRow>
        ) : data && data.length > 0 ? (
          data.map((row: RetrievalRow) => (
            <TableRow key={row.span_id}>
              <TableCell className="max-w-xs truncate font-mono text-xs">{row.query}</TableCell>
              <TableCell className="text-xs">{row.retriever}</TableCell>
              <TableCell>{row.top_k}</TableCell>
              <TableCell>
                <span className={row.recall_at_k > 0 ? "text-green-600" : "text-red-500"}>
                  {row.recall_at_k}
                </span>
              </TableCell>
              <TableCell className="font-mono text-xs">{row.run_id || "—"}</TableCell>
              <TableCell className="text-muted-foreground">
                {formatTimestamp(row.start_time)}
              </TableCell>
              <TableCell>{row.duration_ms}</TableCell>
            </TableRow>
          ))
        ) : (
          <TableRow>
            <TableCell colSpan={7} className="text-center text-muted-foreground">
              No retrieval data yet.
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
