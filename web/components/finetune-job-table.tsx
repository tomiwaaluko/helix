"use client";

import { useState } from "react";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { createFinetuneJob, fetchFinetuneJobs } from "@/lib/api";
import type { FinetuneJob } from "@/lib/types";

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

export function FinetuneJobTable() {
  const queryClient = useQueryClient();
  const [trainSplit, setTrainSplit] = useState("");
  const [evalSplit, setEvalSplit] = useState("");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["finetune-jobs"],
    queryFn: fetchFinetuneJobs,
    staleTime: 30_000,
  });

  const mutation = useMutation({
    mutationFn: () =>
      createFinetuneJob({
        train_split: trainSplit.trim(),
        eval_split: evalSplit.trim(),
      }),
    onSuccess: () => {
      setTrainSplit("");
      setEvalSplit("");
      void queryClient.invalidateQueries({ queryKey: ["finetune-jobs"] });
    },
  });

  if (isError) {
    const msg = (error as Error).message;
    const is503 = msg.includes("503");
    return (
      <p className="text-sm text-muted-foreground">
        {is503 ? "Finetune service not configured." : `Failed to load finetune jobs: ${msg}`}
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-2 items-end">
        <div className="grid gap-1">
          <label className="text-xs font-medium">Train split</label>
          <input
            className="h-8 w-64 rounded border px-2 text-xs"
            placeholder="evals/datasets/hotpotqa_train_1000.jsonl"
            value={trainSplit}
            onChange={(e) => setTrainSplit(e.target.value)}
          />
        </div>
        <div className="grid gap-1">
          <label className="text-xs font-medium">Eval split</label>
          <input
            className="h-8 w-64 rounded border px-2 text-xs"
            placeholder="evals/datasets/hotpotqa_dev_100.jsonl"
            value={evalSplit}
            onChange={(e) => setEvalSplit(e.target.value)}
          />
        </div>
        <Button
          size="sm"
          disabled={!trainSplit.trim() || !evalSplit.trim() || mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending ? "Starting…" : "Run fine-tune"}
        </Button>
        {mutation.isError && (
          <p className="text-xs text-red-600">{(mutation.error as Error).message}</p>
        )}
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>ID</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Outcome</TableHead>
            <TableHead>Failures</TableHead>
            <TableHead>Triplets</TableHead>
            <TableHead>Before</TableHead>
            <TableHead>After</TableHead>
            <TableHead>Δ Recall</TableHead>
            <TableHead>Train split</TableHead>
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
            data.map((job: FinetuneJob) => {
              const delta =
                job.before_recall != null && job.after_recall != null
                  ? job.after_recall - job.before_recall
                  : null;
              return (
                <TableRow key={job.id}>
                  <TableCell className="font-mono text-xs">{job.id.slice(0, 8)}…</TableCell>
                  <TableCell>
                    <StatusBadge status={job.status} />
                  </TableCell>
                  <TableCell className="text-xs">{job.outcome ?? "—"}</TableCell>
                  <TableCell className="tabular-nums">{job.failures ?? "—"}</TableCell>
                  <TableCell className="tabular-nums">{job.triplets ?? "—"}</TableCell>
                  <TableCell className="tabular-nums">{pct(job.before_recall)}</TableCell>
                  <TableCell className="tabular-nums">{pct(job.after_recall)}</TableCell>
                  <TableCell
                    className={`tabular-nums ${delta != null && delta > 0 ? "text-green-600" : delta != null && delta < 0 ? "text-red-600" : ""}`}
                  >
                    {delta != null ? (delta > 0 ? "+" : "") + (delta * 100).toFixed(1) + "%" : "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs max-w-[200px] truncate">
                    {job.train_split}
                  </TableCell>
                </TableRow>
              );
            })
          ) : (
            <TableRow>
              <TableCell colSpan={9} className="text-center text-muted-foreground">
                No finetune jobs yet.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
