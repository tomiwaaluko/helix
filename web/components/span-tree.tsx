"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchTrace } from "@/lib/api";
import type { SpanRecord } from "@/lib/types";
import type { BadgeProps } from "@/components/ui/badge";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

// ── span tree data builder ────────────────────────────────────────────────────

interface SpanNode extends SpanRecord {
  children: SpanNode[];
}

function buildTree(spans: SpanRecord[]): SpanNode[] {
  const byId = new Map<string, SpanNode>();
  for (const s of spans) {
    byId.set(s.span_id, { ...s, children: [] });
  }
  const roots: SpanNode[] = [];
  for (const node of byId.values()) {
    const pid = node.parent_span_id;
    if (pid && byId.has(pid)) {
      byId.get(pid)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

// ── badge variant for span kind ───────────────────────────────────────────────

function kindVariant(kind: string): BadgeProps["variant"] {
  switch (kind) {
    case "task":
      return "default";
    case "llm":
      return "info";
    case "retrieval":
      return "success";
    default:
      return "neutral";
  }
}

// ── single span row (recursive) ───────────────────────────────────────────────

function SpanRow({ node, depth }: { node: SpanNode; depth: number }) {
  const [open, setOpen] = useState(false);
  const hasChildren = node.children.length > 0;
  const hasAttrs = node.attributes && Object.keys(node.attributes).length > 0;

  return (
    <div>
      <div
        className="flex items-center gap-2 py-1.5 px-2 rounded hover:bg-muted/50 cursor-pointer"
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
        onClick={() => setOpen((v) => !v)}
        role="button"
        aria-expanded={open}
      >
        <span className="text-muted-foreground text-xs w-4">
          {hasChildren || hasAttrs ? (open ? "▾" : "▸") : " "}
        </span>
        <Badge variant={kindVariant(node.kind)} className="text-xs">
          {node.kind}
        </Badge>
        <span className="flex-1 text-sm font-medium truncate">{node.name}</span>
        <span className="text-xs text-muted-foreground tabular-nums shrink-0">
          {node.duration_ms} ms
        </span>
      </div>

      {open && (
        <div>
          {hasAttrs && (
            <SpanAttrs attrs={node.attributes as Record<string, string>} depth={depth + 1} />
          )}
          {node.children.map((child) => (
            <SpanRow key={child.span_id} node={child} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── attribute table with lazy blob loading ────────────────────────────────────

function SpanAttrs({ attrs, depth }: { attrs: Record<string, string>; depth: number }) {
  return (
    <dl
      className="text-xs grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 py-1 text-muted-foreground"
      style={{ paddingLeft: `${depth * 20 + 8}px` }}
    >
      {Object.entries(attrs).map(([k, v]) => (
        <AttrRow key={k} name={k} value={v} />
      ))}
    </dl>
  );
}

function AttrRow({ name, value }: { name: string; value: string }) {
  const [payload, setPayload] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const isBlob = value.startsWith("https://") && name.endsWith("_url");

  async function loadBlob() {
    setLoading(true);
    try {
      const text = await fetch(value).then((r) => r.text());
      setPayload(text);
    } catch {
      setPayload("[failed to load payload]");
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <dt className="font-mono">{name}</dt>
      <dd className="break-all">
        {isBlob ? (
          payload !== null ? (
            <pre className="whitespace-pre-wrap text-foreground text-xs bg-muted rounded p-2 mt-1 max-h-64 overflow-auto">
              {payload}
            </pre>
          ) : (
            <button className="underline text-primary" onClick={loadBlob} disabled={loading}>
              {loading ? "Loading…" : "Load payload"}
            </button>
          )
        ) : (
          value
        )}
      </dd>
    </>
  );
}

// ── main SpanTree component ───────────────────────────────────────────────────

export function SpanTree({ runId }: { runId: string }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["trace", runId],
    queryFn: () => fetchTrace(runId),
    retry: false,
    staleTime: Infinity, // traces are immutable once the run finishes
  });

  if (isLoading) {
    return (
      <div className="space-y-2 p-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-7 w-full" />
        ))}
      </div>
    );
  }

  if (isError) {
    const msg = error instanceof Error ? error.message : "Unknown error loading trace";
    if (msg.includes("503")) {
      return (
        <p className="text-sm text-muted-foreground p-4">
          Trace not available — the orchestrator is not connected to ClickHouse. Start the collector
          and set <code className="font-mono">CLICKHOUSE_URL</code> on the orchestrator.
        </p>
      );
    }
    return <p className="text-sm text-destructive p-4">Failed to load trace: {msg}</p>;
  }

  if (!data || data.spans.length === 0) {
    return <p className="text-sm text-muted-foreground p-4">No spans recorded for this run yet.</p>;
  }

  const roots = buildTree(data.spans);

  return (
    <div className="border rounded-md overflow-hidden">
      <div className="bg-muted/30 px-3 py-2 text-xs font-mono text-muted-foreground border-b">
        trace_id: {data.trace_id} &middot; {data.spans.length} span
        {data.spans.length !== 1 ? "s" : ""}
      </div>
      <div className="py-1">
        {roots.map((node) => (
          <SpanRow key={node.span_id} node={node} depth={0} />
        ))}
      </div>
    </div>
  );
}
