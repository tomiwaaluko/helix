import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FinetuneJobTable } from "@/components/finetune-job-table";
import { createFinetuneJob, fetchFinetuneJobs } from "@/lib/api";
import type { FinetuneJob } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({
  fetchFinetuneJobs: vi.fn(),
  createFinetuneJob: vi.fn(),
}));

const mockFetch = vi.mocked(fetchFinetuneJobs);
vi.mocked(createFinetuneJob);

const JOB: FinetuneJob = {
  id: "aaaabbbb-1111-2222-3333-444455556666",
  run_id: "run-001",
  status: "promoted",
  outcome: "promoted",
  train_split: "evals/datasets/hotpotqa_train_1000.jsonl",
  eval_split: "evals/datasets/hotpotqa_dev_100.jsonl",
  corpus_alias: "corpus.active",
  failures: 45,
  triplets: 135,
  before_recall: 0.72,
  after_recall: 0.81,
  created_at: "2026-06-15T10:00:00Z",
  updated_at: "2026-06-15T10:30:00Z",
};

beforeEach(() => {
  mockFetch.mockReset();
});

describe("FinetuneJobTable", () => {
  it("renders a finetune job row", async () => {
    mockFetch.mockResolvedValue([JOB]);
    renderWithClient(<FinetuneJobTable />);
    const promotedEls = await screen.findAllByText("promoted");
    expect(promotedEls.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("45")).toBeInTheDocument();
    expect(screen.getByText("135")).toBeInTheDocument();
  });

  it("shows empty state when no jobs", async () => {
    mockFetch.mockResolvedValue([]);
    renderWithClient(<FinetuneJobTable />);
    expect(await screen.findByText("No finetune jobs yet.")).toBeInTheDocument();
  });

  it("shows 503 error message", async () => {
    mockFetch.mockRejectedValue(new Error("request failed (503): finetune not configured"));
    renderWithClient(<FinetuneJobTable />);
    expect(await screen.findByText(/Finetune service not configured/)).toBeInTheDocument();
  });

  it("shows generic error on other failures", async () => {
    mockFetch.mockRejectedValue(new Error("network timeout"));
    renderWithClient(<FinetuneJobTable />);
    expect(await screen.findByText(/Failed to load finetune jobs/)).toBeInTheDocument();
  });
});
