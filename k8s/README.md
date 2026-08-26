# Mizan.ai on Kubernetes

Local-testing manifests for the same 10 services `docker-compose.yml` runs, converted to plain
Kubernetes resources (Deployments + Services, no Helm). Built against Docker Desktop's built-in
single-node cluster; the same manifests are the starting point for the eventual k3s deployment on
GCP (see the deployment-planning notes) — a few things noted below will need small adjustments
when that day comes (storage class, ingress), not a rewrite.

## One-time setup

1. **Enable Kubernetes** in Docker Desktop: Settings → Kubernetes → check "Enable Kubernetes" →
   Apply & Restart.
2. **Build the app images** (if not already built by `docker compose build`):
   ```
   docker compose build
   ```
   Docker Desktop's Kubernetes shares the same image cache as `docker`, so these are used directly
   — no registry, no push. Every Deployment here uses `imagePullPolicy: IfNotPresent` for exactly
   this reason.
3. **Create the secret** from your existing `.env` (values never get committed to the repo or
   printed anywhere — this reads the file directly):
   ```
   kubectl create namespace mizan
   kubectl create secret generic mizan-secrets -n mizan \
     --from-env-file=.env \
     --from-literal=DB_PASSWORD=postgres123
   ```
   (`DB_PASSWORD` isn't in `.env` — docker-compose.yml hardcodes `postgres123` directly, so it's
   added the same way here. Change it to something real before this ever leaves local testing.)

## Apply everything

```
kubectl apply -k k8s/
```

This creates the `mizan` namespace (if the create above didn't already), the ConfigMap, and all 10
services. `mizan-secrets` is intentionally not part of the kustomization — it's created once by
hand per the step above, so no secret value ever needs to live in a YAML file in this repo.

## Verify

```
kubectl get pods -n mizan
kubectl get svc -n mizan
```

Everything should reach `Running`/`1/1 Ready` within a minute or two. To reach a service from your
machine the way `docker-compose.yml`'s port mappings did, port-forward it, e.g.:

```
kubectl port-forward -n mizan svc/chatbot 8000:8000
```

## What's different from docker-compose.yml, on purpose

- **Internal URLs point at Kubernetes Service names**, not container names — `mizan-postgres`,
  `mizan-qdrant`, `translator`, `doc-extraction` are all named to match exactly what the
  application code already expects (`DB_HOST`, `QDRANT_URL`, `MIZAN_TRANSLATOR_INTERNAL_URL`,
  `MIZAN_DOC_EXTRACTION_URL`), so **no application code changed** — only where those hostnames
  resolve.
- **Postgres and Qdrant are Deployments with `strategy: Recreate`**, not StatefulSets. Single
  replica each, so a StatefulSet's stable-identity guarantees add nothing here; `Recreate` (instead
  of the default `RollingUpdate`) avoids two pods ever trying to mount the same `ReadWriteOnce`
  volume at once during an update.
- **`rag-online`'s SQLite PVC starts empty.** `docker-compose.yml` shared `know_vat_zatca_sqlite`
  with a long-running Qdrant container and the offline ingestion pipeline via an `external: true`
  docker volume — that cross-project sharing is docker-specific and doesn't carry over here as-is.
  Getting the offline-ingested data into this PVC is a separate follow-up, not required for the
  other 9 services to work.
- **Resource requests/limits are estimates**, not measured — doc-extraction (Docling/torch) and
  converter (spawns LibreOffice) are sized larger than the rest since they're the two genuinely
  heavier services; revisit once metrics-server or Prometheus is in place and there's real usage
  data.
- **No Ingress yet.** Local testing uses `kubectl port-forward`; an ingress-nginx + cert-manager
  setup is planned for the GCP deployment once a domain is confirmed, not needed for this local
  step.
- **No `storageClassName` set** on any PVC — relies on Docker Desktop's default `hostpath`
  provisioner. On the eventual k3s/GCP target this resolves to k3s's built-in local-path
  provisioner instead, pointed at the attached persistent disk — same manifests, different
  cluster-level default, no YAML changes expected.
