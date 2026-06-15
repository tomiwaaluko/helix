import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvalDetail } from "@/components/eval-detail";
import { fetchEval } from "@/lib/api";
import type { EvalEvent } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({ fetchEval: vi.fn() }));

const mockFetchEval = vi.mocked(fetchEval);

const EVENTS: EvalEvent[] = [
  {
    eval_id: "eval-1",
    example_id: "ex_000",
    scorer: "exact",
    score: 1.0,
    passed: true,
    timestamp: "2026-06-14T10:01:00Z",
  },
  {
    eval_id: "eval-1",
    example_id: "ex_000",
    scorer: "f1",
    score: 0.75,
    passed: false,
    timestamp: "2026-06-14T10:01:00Z",
  },
  {
    eval_id: "eval-1",
    example_id: "ex_001",
    scorer: "exact",
    score: 0.0,
    passed: false,
    timestamp: "2026-06-14T10:01:30Z",
  },
  {
    eval_id: "eval-1",
    example_id: "ex_001",
    scorer: "f1",
    score: 0.5,
    passed: false,
    timestamp: "2026-06-14T10:01:30Z",
  },
];

beforeEach(() => {
  mockFetchEval.mockReset();
});

describe("EvalDetail", () => {
  it("renders metric cards and per-example rows", async () => {
    mockFetchEval.mockResolvedValue(EVENTS);
    renderWithClient(<EvalDetail id="eval-1" />);

    // Metric cards: exact mean = 0.500, f1 mean = 0.625
    // "0.500" also appears in the per-example table (ex_001/f1), so use getAllByText.
    expect((await screen.findAllByText("0.500")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("0.625")).toBeInTheDocument();

    // Per-example rows
    expect(screen.getByText("ex_000")).toBeInTheDocument();
    expect(screen.getByText("ex_001")).toBeInTheDocument();
  });

  it("shows 503 message on service unavailable error", async () => {
    mockFetchEval.mockRejectedValue(new Error("request failed (503): clickhouse not set"));
    renderWithClient(<EvalDetail id="eval-1" />);
    expect(
      await screen.findByText(/ClickHouse is not configured/),
    ).toBeInTheDocument();
  });

  it("shows generic error on other failures", async () => {
    mockFetchEval.mockRejectedValue(new Error("network timeout"));
    renderWithClient(<EvalDetail id="eval-1" />);
    expect(await screen.findByText(/Failed to load eval/)).toBeInTheDocument();
  });
});
