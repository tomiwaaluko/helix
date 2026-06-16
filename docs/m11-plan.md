# M11 Plan — Helm/Kubernetes Deployment

> **Status: IN PROGRESS.**

## Goal

Package the full Helix stack as a Helm chart so a single `helm install` spins up every
component on a Kubernetes cluster. The chart targets the same service topology as
`infra/compose/docker-compose.yml` but with production-grade patterns: rolling updates,
resource limits, liveness/readiness probes, secrets from a Kubernetes Secret, and
configurable replica counts for the orchestrator and worker pools.

## Scope

### In scope

- `infra/helm/helix/` — one umbrella chart with sub-charts (or templates per component):
  - **orchestrator** — Go binary; Deployment + Service + HPA stub
  - **collector** — Go binary; Deployment + Service
  - **worker** — Python worker; Deployment (pool name as a template value)
  - **postgresql** — StatefulSet + PVC; or sub-chart alias for Bitnami postgres
  - **nats** — StatefulSet or Bitnami NATS sub-chart
  - **clickhouse** — StatefulSet + PVC (single-shard, not a cluster)
  - **redis** — Deployment or Bitnami redis sub-chart
  - **minio** — StatefulSet + PVC + console Service
  - **qdrant** — StatefulSet + PVC
- `values.yaml` with sensible defaults for local/dev cluster usage
- `values-prod.yaml` overrides for production (higher replica counts, resource limits)
- Secrets: `helix-secrets` Kubernetes Secret template (API token, DB URL, etc.)
- `infra/helm/README.md` — how to install + first-run steps (migrate, seed)

### Out of scope

- Ingress / TLS termination (add separately per cluster)
- Monitoring stack (Prometheus/Grafana — chart annotations prepared but stack not bundled)
- Multi-region / multi-shard ClickHouse

## Chart structure

```
infra/helm/
  helix/
    Chart.yaml
    values.yaml
    values-prod.yaml
    templates/
      _helpers.tpl
      secrets.yaml
      orchestrator/
        deployment.yaml
        service.yaml
        hpa.yaml
      collector/
        deployment.yaml
        service.yaml
      worker/
        deployment.yaml        (parameterized by pool name)
      postgresql/
        statefulset.yaml
        service.yaml
        pvc.yaml
      nats/
        statefulset.yaml
        service.yaml
      clickhouse/
        statefulset.yaml
        service.yaml
        pvc.yaml
      redis/
        deployment.yaml
        service.yaml
      minio/
        statefulset.yaml
        service.yaml           (API + console)
        pvc.yaml
      qdrant/
        statefulset.yaml
        service.yaml
        pvc.yaml
  README.md
```
