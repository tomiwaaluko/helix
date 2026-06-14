import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

// POST /api/runs/:id/cancel — cancel a run.
export async function POST(
  _req: Request,
  { params }: { params: { id: string } },
): Promise<Response> {
  const res = await orchestratorFetch(`/api/v1/runs/${encodeURIComponent(params.id)}/cancel`, {
    method: "POST",
  });
  return proxyJSON(res);
}
