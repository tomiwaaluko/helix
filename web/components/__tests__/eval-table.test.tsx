import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvalTable } from "@/components/eval-table";
import { fetchEvals } from "@/lib/api";
import type { EvalSummary } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({ fetchEvals: vi.fn() }));

const mockFetchEvals = vi.mocked(fetchEvals);

const EVAL: EvalSummary = {
  eval_id: "eval-42",
  examples: 10,
  started_at: "2026-06-14T10:00:00Z",
  finished_at: "2026-06-14T10:05:00Z",
  scorers: [
    { scorer: "exact", mean: 0.9, n: 10 },
    { scorer: "f1", mean: 0.85, n: 10 },
  ],
};

beforeEach(() => {
  mockFetchEvals.mockReset();
});

describe("EvalTable", () => {
  it("renders an eval row with scorer columns", async () => {
    mockFetchEvals.mockResolvedValue([EVAL]);
    renderWithClient(<EvalTable />);
    expect(await screen.findByText("eval-42")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("0.900")).toBeInTheDocument();
    expect(screen.getByText("0.850")).toBeInTheDocument();
  });

  it("shows empty state when no evals", async () => {
    mockFetchEvals.mockResolvedValue([]);
    renderWithClient(<EvalTable />);
    expect(await screen.findByText("No eval runs yet.")).toBeInTheDocument();
  });

  it("shows 503 message on service unavailable error", async () => {
    mockFetchEvals.mockRejectedValue(new Error("request failed (503): clickhouse not set"));
    renderWithClient(<EvalTable />);
    expect(
      await screen.findByText(/ClickHouse is not configured/),
    ).toBeInTheDocument();
  });

  it("shows generic error message on other failures", async () => {
    mockFetchEvals.mockRejectedValue(new Error("network down"));
    renderWithClient(<EvalTable />);
    expect(await screen.findByText(/Failed to load evals/)).toBeInTheDocument();
  });
});
