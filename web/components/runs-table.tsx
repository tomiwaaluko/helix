"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { StatusBadge } from "@/components/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchRuns } from "@/lib/api";
import type { RunStatus } from "@/lib/types";
import { formatTimestamp } from "@/lib/utils";

const STATUSES: RunStatus[] = ["pending", "running", "succeeded", "failed", "cancelled"];

export function RunsTable() {
  const [status, setStatus] = useState<string>("");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["runs", status],
    queryFn: () => fetchRuns(status || undefined),
    refetchInterval: 3000,
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <label htmlFor="status" className="text-sm text-muted-foreground">
          Status
        </label>
        <select
          id="status"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="h-8 rounded-md border border-input bg-background px-2 text-sm"
        >
          <option value="">all</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {isError ? (
        <p className="text-sm text-destructive">Failed to load runs: {(error as Error).message}</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead>Workflow</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Submitted by</TableHead>
              <TableHead>Submitted at</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={5}>
                  <Skeleton className="h-6 w-full" />
                </TableCell>
              </TableRow>
            ) : data && data.length > 0 ? (
              data.map((run) => (
                <TableRow key={run.id}>
                  <TableCell>
                    <Link href={`/runs/${run.id}`} className="font-mono text-sm hover:underline">
                      {run.id}
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{run.workflow_id}</TableCell>
                  <TableCell>
                    <StatusBadge status={run.status} />
                  </TableCell>
                  <TableCell>{run.submitted_by || "—"}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {formatTimestamp(run.submitted_at)}
                  </TableCell>
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  No runs yet.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
