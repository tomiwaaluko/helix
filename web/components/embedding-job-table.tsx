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
import { fetchEmbeddingJobs } from "@/lib/api";
import type { EmbeddingJob } from "@/lib/types";

function StatusBadge({ status }: { status: string }) {
  const colour =
    status === "promoted"
      ? "text-green-600"
      : status === "archived" || status === "no_failures" || status === "no_triplets"
        ? "text-yellow-600"
        : status === "failed"
          ? "text-red-600"
          : "text-muted-foreground";
  return <span className={`text-xs font-semibold ${colour}`}>{status}</span>;
}

function pct(v: number | undefined | null): string {
  if (v == null) return "—";
  return (v * 100).toFixed(1) + "%";
}

export function EmbeddingJobTable() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["embedding-jobs"],
    queryFn: fetchEmbeddingJobs,
    staleTime: 30_000,
  });

  if (isError) {
    const msg = (error as Error).message;
    const is503 = msg.includes("503");
    return (
      <p className="text-sm text-muted-foreground">
        {is503 ? "Embedding service not configured." : `Failed to load embedding jobs: ${msg}`}
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>ID</TableHead>
          <TableHead>Base Model</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Triplets</TableHead>
          <TableHead>Before %</TableHead>
          <TableHead>After %</TableHead>
          <TableHead>Δ Recall</TableHead>
          <TableHead>Promoted At</TableHead>
          <TableHead>Artifact</TableHead>
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
          data.map((job: EmbeddingJob) => {
            const metrics = job.metrics as
              | Record<string, Record<string, number>>
              | null
              | undefined;
            const before = metrics?.before?.mean ?? null;
            const after = metrics?.after?.mean ?? null;
            const delta = before != null && after != null ? after - before : null;
            return (
              <TableRow key={job.id}>
                <TableCell className="font-mono text-xs">{job.id.slice(0, 8)}…</TableCell>
                <TableCell className="text-xs">{job.base_model}</TableCell>
                <TableCell>
                  <StatusBadge status={job.status} />
                </TableCell>
                <TableCell className="tabular-nums">{job.triplets_count ?? "—"}</TableCell>
                <TableCell className="tabular-nums">{pct(before)}</TableCell>
                <TableCell className="tabular-nums">{pct(after)}</TableCell>
                <TableCell
                  className={`tabular-nums ${delta != null && delta > 0 ? "text-green-600" : delta != null && delta < 0 ? "text-red-600" : ""}`}
                >
                  {delta != null ? (delta > 0 ? "+" : "") + (delta * 100).toFixed(1) + "%" : "—"}
                </TableCell>
                <TableCell className="text-xs">
                  {job.promoted_at ? new Date(job.promoted_at).toLocaleDateString() : "—"}
                </TableCell>
                <TableCell className="text-xs">
                  {job.artifact_uri ? (
                    <a
                      href={job.artifact_uri}
                      className="text-blue-600 underline"
                      target="_blank"
                      rel="noreferrer"
                    >
                      download
                    </a>
                  ) : (
                    "—"
                  )}
                </TableCell>
              </TableRow>
            );
          })
        ) : (
          <TableRow>
            <TableCell colSpan={9} className="text-center text-muted-foreground">
              No embedding jobs yet.
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
