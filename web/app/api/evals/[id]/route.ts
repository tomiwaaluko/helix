import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

// GET /api/evals/:id — proxies per-example eval events from the orchestrator.
export async function GET(
  _req: Request,
  { params }: { params: { id: string } },
): Promise<Response> {
  const res = await orchestratorFetch(`/api/v1/evals/${encodeURIComponent(params.id)}`);
  return proxyJSON(res);
}
