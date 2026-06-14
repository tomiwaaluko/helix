import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

// GET /api/runs/:id — run detail (including task tree).
export async function GET(
  _req: Request,
  { params }: { params: { id: string } },
): Promise<Response> {
  const res = await orchestratorFetch(`/api/v1/runs/${encodeURIComponent(params.id)}`);
  return proxyJSON(res);
}
