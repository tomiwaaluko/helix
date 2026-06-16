import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "@/components/status-badge";

describe("StatusBadge", () => {
  it("renders the status text", () => {
    render(<StatusBadge status="succeeded" />);
    expect(screen.getByText("succeeded")).toBeInTheDocument();
  });

  it("uses the success variant for succeeded", () => {
    render(<StatusBadge status="succeeded" />);
    expect(screen.getByText("succeeded").className).toContain("green");
  });

  it("uses the destructive variant for failed", () => {
    render(<StatusBadge status="failed" />);
    expect(screen.getByText("failed").className).toContain("red");
  });
});
