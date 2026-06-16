import { describe, expect, it } from "vitest";

import { cn, decodeJsonBytes, formatTimestamp } from "@/lib/utils";

describe("cn", () => {
  it("merges and dedupes tailwind classes", () => {
    expect(cn("p-2", "p-4")).toBe("p-4");
    expect(cn("text-sm", false && "hidden", "font-bold")).toBe("text-sm font-bold");
  });
});

describe("decodeJsonBytes", () => {
  it("returns empty string for undefined", () => {
    expect(decodeJsonBytes(undefined)).toBe("");
  });

  it("decodes base64 JSON and pretty-prints it", () => {
    const b64 = btoa(JSON.stringify({ question: "who?" }));
    expect(decodeJsonBytes(b64)).toBe('{\n  "question": "who?"\n}');
  });

  it("falls back to the raw value when not decodable JSON", () => {
    expect(decodeJsonBytes("not-base64-json!!")).toBe("not-base64-json!!");
  });
});

describe("formatTimestamp", () => {
  it("returns the raw string for an invalid date", () => {
    expect(formatTimestamp("not-a-date")).toBe("not-a-date");
  });

  it("formats a valid ISO timestamp", () => {
    expect(formatTimestamp("2026-06-14T12:00:00Z")).not.toBe("2026-06-14T12:00:00Z");
  });
});
