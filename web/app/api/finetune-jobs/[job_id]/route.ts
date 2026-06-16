import { orchestratorFetch, proxyJSON } from "@/lib/orchestrator";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ job_id: string }> },
): Promise<Response> {
  const { job_id } = await params;
  const res = await orchestratorFetch(`/api/v1/finetune-jobs/${encodeURIComponent(job_id)}`);
  return proxyJSON(res);
}
