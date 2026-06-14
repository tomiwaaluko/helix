"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cancelRun, fetchRun } from "@/lib/api";
import type { RunStatus } from "@/lib/types";
import { decodeJsonBytes, formatTimestamp } from "@/lib/utils";

const TERMINAL: RunStatus[] = ["succeeded", "failed", "cancelled"];

function JsonBlock({ label, value }: { label: string; value: string | undefined }) {
  const text = decodeJsonBytes(value);
  if (!text) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <pre className="overflow-auto rounded-md bg-muted p-3 text-xs">{text}</pre>
    </div>
  );
}

export function RunDetail({ id }: { id: string }) {
  const qc = useQueryClient();
  const {
    data: run,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["run", id],
    queryFn: () => fetchRun(id),
    refetchInterval: 3000,
  });

  const cancel = useMutation({
    mutationFn: () => cancelRun(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", id] }),
  });

  if (isLoading) return <Skeleton className="h-40 w-full" />;
  if (isError)
    return (
      <p className="text-sm text-destructive">Failed to load run: {(error as Error).message}</p>
    );
  if (!run) return null;

  const canCancel = !TERMINAL.includes(run.status);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="font-mono text-base">{run.id}</CardTitle>
          <div className="flex items-center gap-3">
            <StatusBadge status={run.status} />
            {canCancel && (
              <Button
                variant="destructive"
                size="sm"
                onClick={() => cancel.mutate()}
                disabled={cancel.isPending}
              >
                {cancel.isPending ? "Cancelling…" : "Cancel"}
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-3 text-sm">
          <Field label="Workflow" value={run.workflow_id} mono />
          <Field label="Trace" value={run.trace_id} mono />
          <Field label="Submitted by" value={run.submitted_by || "—"} />
          <Field label="Submitted at" value={formatTimestamp(run.submitted_at)} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Tasks</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Node</TableHead>
                <TableHead>Task</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Attempts</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {run.tasks.length > 0 ? (
                run.tasks.map((task) => (
                  <TableRow key={task.id}>
                    <TableCell>{task.node_id}</TableCell>
                    <TableCell className="font-mono text-xs">{task.id}</TableCell>
                    <TableCell>
                      <StatusBadge status={task.status} />
                    </TableCell>
                    <TableCell>{task.attempts}</TableCell>
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell colSpan={4} className="text-center text-muted-foreground">
                    No tasks.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {(run.input || run.output || run.error) && (
        <Card>
          <CardContent className="space-y-3 pt-4">
            <JsonBlock label="Input" value={run.input} />
            <JsonBlock label="Output" value={run.output} />
            <JsonBlock label="Error" value={run.error} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className={mono ? "font-mono text-xs" : ""}>{value}</p>
    </div>
  );
}
