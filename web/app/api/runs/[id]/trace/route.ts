import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

// GET /api/runs/:id/trace — span tree for a run (proxied from orchestrator).
// Returns 503 when ClickHouse is not configured on the orchestrator.
export async function GET(
  _req: Request,
  { params }: { params: { id: string } },
): Promise<Response> {
  const res = await orchestratorFetch(`/api/v1/runs/${encodeURIComponent(params.id)}/trace`);
  return proxyJSON(res);
}
