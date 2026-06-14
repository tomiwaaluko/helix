// Client-side data layer: calls the same-origin BFF (app/api/**), never the
// orchestrator directly. Used by TanStack Query hooks in client components.
import type { Run, RunDetail } from "@/lib/types";

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
