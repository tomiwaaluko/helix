import { Badge, type BadgeProps } from "@/components/ui/badge";
import type { RunStatus, TaskStatus } from "@/lib/types";

const VARIANT: Record<string, NonNullable<BadgeProps["variant"]>> = {
  pending: "neutral",
  ready: "info",
  running: "info",
  succeeded: "success",
  failed: "destructive",
  cancelled: "warning",
  dead: "destructive",
};

export function StatusBadge({ status }: { status: RunStatus | TaskStatus }) {
  return <Badge variant={VARIANT[status] ?? "neutral"}>{status}</Badge>;
}
