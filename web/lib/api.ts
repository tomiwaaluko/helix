// Client-side data layer: calls the same-origin BFF (app/api/**), never the
// orchestrator directly. Used by TanStack Query hooks in client components.
import type {
  CreateFinetuneJobRequest,
  EvalEvent,
  EvalSummary,
  FinetuneJob,
  LlmCallRow,
  RetrievalRow,
  Run,
  RunDetail,
  TraceResponse,
} from "@/lib/types";

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`request failed (${res.status}): ${detail}`);
  }
  return res.json() as Promise<T>;
}

export function fetchRuns(status?: string): Promise<Run[]> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return getJSON<Run[]>(`/api/runs${qs}`);
}

export function fetchRun(id: string): Promise<RunDetail> {
  return getJSON<RunDetail>(`/api/runs/${encodeURIComponent(id)}`);
}

export async function cancelRun(id: string): Promise<void> {
  const res = await fetch(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  if (!res.ok) {
    throw new Error(`cancel failed (${res.status})`);
  }
}

export function fetchTrace(id: string): Promise<TraceResponse> {
  return getJSON<TraceResponse>(`/api/runs/${encodeURIComponent(id)}/trace`);
}

export function fetchEvals(): Promise<EvalSummary[]> {
  return getJSON<EvalSummary[]>("/api/evals");
}

export function fetchEval(id: string): Promise<EvalEvent[]> {
  return getJSON<EvalEvent[]>(`/api/evals/${encodeURIComponent(id)}`);
}

export function fetchRetrievals(runId?: string): Promise<RetrievalRow[]> {
  const qs = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  return getJSON<RetrievalRow[]>(`/api/retrievals${qs}`);
}

export function fetchLlmCalls(runId?: string): Promise<LlmCallRow[]> {
  const qs = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  return getJSON<LlmCallRow[]>(`/api/llm-calls${qs}`);
}

export function fetchFinetuneJobs(): Promise<FinetuneJob[]> {
  return getJSON<FinetuneJob[]>("/api/finetune-jobs");
}

export function fetchFinetuneJob(jobId: string): Promise<FinetuneJob> {
  return getJSON<FinetuneJob>(`/api/finetune-jobs/${encodeURIComponent(jobId)}`);
}

export async function createFinetuneJob(body: CreateFinetuneJobRequest): Promise<FinetuneJob> {
  const res = await fetch("/api/finetune-jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`createFinetuneJob failed (${res.status}): ${detail}`);
  }
  return res.json() as Promise<FinetuneJob>;
}
