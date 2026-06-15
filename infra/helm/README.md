# Helix Helm Chart

Production-ready Helm chart for the full Helix stack. Covers all nine
components that are defined in `infra/compose/docker-compose.yml`.

## Components

| Component | Kind | Notes |
|-----------|------|-------|
| orchestrator | Deployment | Go REST + gRPC control plane |
| collector | Deployment | Go OTel/OTLP span ingestion |
| worker (research) | Deployment | Python deep_research pool |
| worker (finetune_job) | Deployment | Python finetune-job pool |
| postgresql | StatefulSet | Run/task/embedding state |
| nats | StatefulSet | JetStream task dispatch |
| clickhouse | StatefulSet | Spans, retrievals, LLM calls |
| redis | Deployment | Exactly-once sentinel + rate limiter |
| minio | StatefulSet | Large span-payload blobs |
| qdrant | StatefulSet | Vector index |

## Prerequisites

- Kubernetes 1.25+
- Helm 3.10+
- PersistentVolume provisioner available in the cluster (for StatefulSet PVCs)

## Quick start (dev/local)

```bash
helm install helix infra/helm/helix \
  -n helix \
  --create-namespace \
  --set secrets.apiToken=<your-token>
```

## Production install

```bash
helm install helix infra/helm/helix \
  -n helix \
  --create-namespace \
  -f infra/helm/helix/values-prod.yaml \
  --set secrets.apiToken=<your-token> \
  --set secrets.postgresPassword=<strong-password> \
  --set secrets.minioRootPassword=<strong-password>
```

## First-time setup

After all pods are running:

1. Check pod status:

   ```bash
   kubectl get pods -n helix
   ```

2. Run database migrations (the orchestrator also runs them at startup,
   but you can trigger manually):

   ```bash
   kubectl exec -n helix deploy/helix-orchestrator \
     -- /app/orchestrator migrate
   ```

3. Seed the corpus and build the Qdrant index:

   ```bash
   kubectl exec -n helix deploy/helix-worker \
     -- python -m helix.scripts.seed
   ```

## Accessing services

Port-forward the REST API:

```bash
kubectl port-forward -n helix svc/helix-orchestrator 8080:8080
```

Port-forward the gRPC endpoint:

```bash
kubectl port-forward -n helix svc/helix-orchestrator 50051:50051
```

Port-forward the MinIO console:

```bash
kubectl port-forward -n helix svc/helix-minio 9101:9101
# Open http://localhost:9101 in your browser
```

## Upgrading

```bash
helm upgrade helix infra/helm/helix \
  -n helix \
  -f infra/helm/helix/values-prod.yaml \
  --set secrets.apiToken=<your-token>
```

## Uninstalling

```bash
helm uninstall helix -n helix
# PVCs are NOT deleted automatically; remove them manually if desired:
kubectl delete pvc -n helix -l app.kubernetes.io/instance=helix
```

## Key values

| Key | Default | Description |
|-----|---------|-------------|
| `secrets.apiToken` | `dev-token` | HELIX_API_TOKEN (required) |
| `secrets.postgresPassword` | `helix` | PostgreSQL password |
| `secrets.minioRootPassword` | `helixhelix` | MinIO root password |
| `orchestrator.replicaCount` | `1` | Orchestrator pod count |
| `worker.replicaCount` | `1` | Research worker pod count |
| `worker.finetune.replicaCount` | `1` | Finetune worker pod count |
| `postgresql.storage` | `10Gi` | Postgres PVC size |
| `clickhouse.storage` | `20Gi` | ClickHouse PVC size |
| `minio.storage` | `20Gi` | MinIO PVC size |
| `qdrant.storage` | `10Gi` | Qdrant PVC size |

For the full list see `infra/helm/helix/values.yaml`. Production overrides
(higher replicas, resource requests/limits, storageClassName) are in
`infra/helm/helix/values-prod.yaml`.
