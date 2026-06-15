// Friendly aliases over the generated OpenAPI types. Regenerate api-types.ts with
// `npm run gen:types` after editing openapi.yaml — do not hand-edit api-types.ts.
import type { components } from "@/lib/api-types";

export type Run = components["schemas"]["Run"];
export type Task = components["schemas"]["Task"];
export type RunDetail = components["schemas"]["RunDetail"];
export type RunStatus = components["schemas"]["RunStatus"];
export type TaskStatus = components["schemas"]["TaskStatus"];
export type SpanRecord = components["schemas"]["SpanRecord"];
export type TraceResponse = components["schemas"]["TraceResponse"];
export type ScorerMean = components["schemas"]["ScorerMean"];
export type EvalSummary = components["schemas"]["EvalSummary"];
export type EvalEvent = components["schemas"]["EvalEvent"];
