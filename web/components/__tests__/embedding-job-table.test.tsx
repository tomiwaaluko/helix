import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EmbeddingJobTable } from "@/components/embedding-job-table";
import { fetchEmbeddingJobs } from "@/lib/api";
import type { EmbeddingJob } from "@/lib/types";
import { renderWithClient } from "@/test/render";

vi.mock("@/lib/api", () => ({
  fetchEmbeddingJobs: vi.fn(),
}));

const mockFetch = vi.mocked(fetchEmbeddingJobs);

const JOB: EmbeddingJob = {
  id: "ccccdddd-1111-2222-3333-444455556666",
  finetune_job_id: "aaaabbbb-1111-2222-3333-444455556666",
  base_model: "nomic-ai/nomic-embed-text-v1.5",
  status: "promoted",
  triplets_count: 135,
  metrics: { before: { mean: 0.72 }, after: { mean: 0.81 } },
  artifact_uri: null,
  created_at: "2026-06-15T10:00:00Z",
  promoted_at: "2026-06-15T10:30:00Z",
};

beforeEach(() => {
  mockFetch.mockReset();
});

describe("EmbeddingJobTable", () => {
  it("renders an embedding job row", async () => {
    mockFetch.mockResolvedValue([JOB]);
    renderWithClient(<EmbeddingJobTable />);
    const promotedEls = await screen.findAllByText("promoted");
    expect(promotedEls.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("135")).toBeInTheDocument();
  });

  it("shows empty state when no jobs", async () => {
    mockFetch.mockResolvedValue([]);
    renderWithClient(<EmbeddingJobTable />);
    expect(await screen.findByText("No embedding jobs yet.")).toBeInTheDocument();
  });

  it("shows 503 error message", async () => {
    mockFetch.mockRejectedValue(new Error("request failed (503): embedding not configured"));
    renderWithClient(<EmbeddingJobTable />);
    expect(await screen.findByText(/Embedding service not configured/)).toBeInTheDocument();
  });

  it("shows generic error on other failures", async () => {
    mockFetch.mockRejectedValue(new Error("network timeout"));
    renderWithClient(<EmbeddingJobTable />);
    expect(await screen.findByText(/Failed to load embedding jobs/)).toBeInTheDocument();
  });
});
