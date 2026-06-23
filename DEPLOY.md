# Deploying the PR Health Dashboard with Harness CD

This deploys the dashboard as a web app to your Kubernetes cluster, using
**Harness CI** to build the image and **Harness CD** to roll it out — giving you
a live URL to show.

```
 code → Harness CI (build image) → registry → Harness CD (deploy) → K8s → URL
```

## 0. Test locally first (1 min, no Harness)

```bash
docker build -t pr-health .
docker run -p 8080:8080 pr-health          # serves bundled sample data
open http://localhost:8080
```
For live data locally: `docker run -p 8080:8080 --env-file .env pr-health`.

## Prerequisites (one-time Harness setup)

| # | What | Where in Harness |
|---|------|------------------|
| 1 | **Delegate** installed *in your K8s cluster* | Project Setup → Delegates |
| 2 | **Kubernetes connector** (uses that delegate) | Connectors → + New → Kubernetes |
| 3 | **Docker registry connector** (Docker Hub/GCR/ECR) | Connectors → + New → Docker Registry |
| 4 | **Secret** `harness_pat` = your Harness API token | Project Setup → Secrets |
| 5 | Code in a repo Harness can build (Harness Code repo is simplest) | Code |

## Steps

### 1. Get the code + manifests into your repo
Commit `pr_health.py`, `sample_data.json`, `Dockerfile`, and `k8s/deployment.yaml`
to the repo Harness will build.

### 2. Create a Harness **Service**
- Deployment type: **Kubernetes**
- Manifest: **K8s Manifest** → source = your repo → path = `k8s/deployment.yaml`
- Artifact: your **Docker registry connector** + image (e.g. `yourname/pr-health`).
  The manifest's `image: <+artifact.image>` resolves to this.

### 3. Inject the API token into the pod
In `k8s/deployment.yaml`, the `pr-health-secrets` Secret holds `HARNESS_API_KEY`.
Set it from your Harness secret so it's never hard-coded:
```yaml
stringData:
  HARNESS_API_KEY: <+secret.getValue("harness_pat")>
```
(Or skip live data entirely — with no token the pod serves sample data.)

### 4. Create an **Environment** + **Infrastructure**
- Environment: e.g. `dev`
- Infrastructure: **Kubernetes** → your K8s connector → namespace `pr-health`

### 5. Create the pipeline
Import `harness/pipeline.yaml` (Pipelines → + New → Import from Git, or paste in
the YAML editor). Fill the `<+input>` fields:
- codebase connector + repoName (CI clone)
- Docker registry connector + image repo (build/push)
- service / environment / infrastructure refs (deploy)

### 6. Run it
Pipelines → **Run**. CI builds and pushes the image; CD does a rolling deploy.

### 7. Open the live URL
```bash
kubectl get svc -n pr-health pr-health
# use the EXTERNAL-IP of the LoadBalancer:
open http://<EXTERNAL-IP>
```
(No external IP? Your cluster may need an ingress controller, or use
`kubectl port-forward -n pr-health svc/pr-health 8080:80` to demo.)

## Live PR events (webhook trigger)

The deployed app also **receives webhooks**: whenever a PR is **opened** or
**merged to main/master**, Harness Code POSTs the event to the app, which looks up
the PR's full details + checks and shows it in a **"Live PR events"** feed on the
dashboard.

Endpoint: `POST http://<your-app-URL>/webhook`

### Register the webhook in Harness Code
1. Open the repo (`harness-core`) → **Settings → Webhooks → + New Webhook**.
2. **Payload URL:** `http://<EXTERNAL-IP>/webhook` (your deployed Service URL).
3. **Events:** select **Pull Request → Created** and **Pull Request → Merged**
   (Branch → Updated too, if you want raw pushes to main).
4. Content type: `application/json`. Save.

Now open or merge a PR to main and watch it appear in the dashboard's events feed.
The app filters to PRs targeting `main`/`master`; others are acknowledged but not shown.

### Test it without a real PR
```bash
curl -X POST http://<EXTERNAL-IP>/webhook -H 'Content-Type: application/json' \
  -d '{"trigger":"pullreq_created","pull_req":{"number":8,"title":"demo","target_branch":"main"}}'
```

> Note: the webhook needs a publicly reachable URL — that's why we deploy it. For
> local testing, expose it with `ngrok http 8080` and use the ngrok URL.

## Give the repo a real PR check (so build ✓/✗ is meaningful)

`pr-health-check-group-6` has no CI, so every PR shows "no CI checks". To get a
real green/red build status, add a CI pipeline that runs on PRs:

1. **Project `test_project_vp` → Pipelines → + Create → CI.**
2. **Codebase:** choose the Harness Code repo `pr-health-check-group-6`.
3. **Build infra:** Cloud (hosted) — no delegate needed.
4. Add a **Run** step (the YAML in `harness/pr-check-pipeline.yaml` is ready to paste
   via the pipeline's YAML editor). Its command exits 0 (pass); uncomment `exit 1`
   to demo a failing build.
5. **Add a Trigger:** Triggers → + New Trigger → **Webhook → Harness Code** →
   events **Pull Request: Created / Updated** (+ Push for merges to main).
6. Save. Now opening/updating a PR runs the pipeline, and Harness posts its result
   as a **status check** on the PR.

Re-trigger a PR → the analyzer (and the live feed) will show **build ✓** (or
**build ✗ failed** with the check name) instead of "no CI checks".

## Make it refresh automatically (the "nobody reads it" fix)
Add a **Cron trigger** to the pipeline (e.g. daily) so the dashboard image is
rebuilt with fresh data and redeployed — the report stays current with no manual work.

## Troubleshooting
- **CI can't clone** → check the codebase connector + repoName.
- **Push denied** → Docker registry connector creds / image path.
- **Deploy stuck / ImagePullBackOff** → the cluster can't pull from your registry;
  add an imagePullSecret or use a public image.
- **Page shows sample data, not live** → `HARNESS_API_KEY` not reaching the pod
  (check the secret), or the token lacks Code read permission.
