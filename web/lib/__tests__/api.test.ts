import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => vi.restoreAllMocks());

describe("fetchEvals", () => {
  it("calls /api/evals and returns parsed JSON", async () => {
    const payload = [
      { eval_id: "eval-1", examples: 5, started_at: "", finished_at: "", scorers: [] },
    ];
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 200 }),
    );
    const { fetchEvals } = await import("@/lib/api");
    const result = await fetchEvals();
    expect(fetch).toHaveBeenCalledWith("/api/evals");
    expect(result[0].eval_id).toBe("eval-1");
  });

  it("throws on non-2xx responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("not configured", { status: 503 }),
    );
    const { fetchEvals } = await import("@/lib/api");
    await expect(fetchEvals()).rejects.toThrow("503");
  });
});

describe("fetchEval", () => {
  it("calls /api/evals/:id and returns parsed JSON", async () => {
    const payload = [
      {
        eval_id: "eval-1",
        example_id: "ex_000",
        scorer: "exact",
        score: 1.0,
        passed: true,
        timestamp: "",
      },
    ];
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 200 }),
    );
    const { fetchEval } = await import("@/lib/api");
    const result = await fetchEval("eval-1");
    expect(fetch).toHaveBeenCalledWith("/api/evals/eval-1");
    expect(result[0].example_id).toBe("ex_000");
  });
});

describe("fetchTrace", () => {
  it("calls the trace BFF path and returns parsed JSON", async () => {
    const payload = { trace_id: "trace-abc", spans: [] };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 200 }),
    );

    const { fetchTrace } = await import("@/lib/api");
    const result = await fetchTrace("run-xyz");

    expect(fetch).toHaveBeenCalledWith("/api/runs/run-xyz/trace");
    expect(result.trace_id).toBe("trace-abc");
  });

  it("throws on non-2xx responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("not configured", { status: 503 }),
    );

    const { fetchTrace } = await import("@/lib/api");
    await expect(fetchTrace("run-xyz")).rejects.toThrow("503");
  });
});
