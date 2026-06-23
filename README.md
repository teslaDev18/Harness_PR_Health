# PR Health Analyzer

Reads pull-request data from Harness SCM and surfaces unhealthy engineering
practices, ranking repositories by how often they break PR-quality rules.

It answers the three questions from the brief:
- **How healthy are our PR practices?** — the signal table
- **Which repos need attention now?** — the ranked list + drill-down
- **What behaviours correlate with quality?** — per-signal breakdown

## Signals detected

| Signal | Rule |
|--------|------|
| `merged_without_passing_build` | merged with no checks, or a failing/failed check *(the MVP signal)* |
| `no_linked_jira_ticket` | no `PROJ-123`-style ticket key in title/branch/description |
| `merged_too_fast` | merged < 10 min after opening (rushed, likely unreviewed) |
| `large_change_low_review` | > 400 lines changed with fewer than 1 approval |

Thresholds live at the top of `pr_health.py` and are easy to tune.

## Run it

```bash
# 1. On the bundled sample data (no setup needed):
python3 pr_health.py

# 2. Machine-readable output:
python3 pr_health.py --json

# 2b. Visual HTML dashboard (write to a file, open in a browser):
python3 pr_health.py --html report.html

# 2c. Run as a web app (what gets deployed):
python3 pr_health.py --serve            # then open http://localhost:8080

# 3. On live Harness data:
export HARNESS_API_KEY=...        # a Harness personal/API token
export HARNESS_ACCOUNT_ID=9UuUfLwaQ-6ZowvbG7qtLQ
export HARNESS_ORG=default
export HARNESS_PROJECT=CIBootcamp2026
pip install requests
python3 pr_health.py --live
```

## Which repositories / teams need attention right now?

Every run prints an **Attention** section that answers the brief's second question:

- Each repo gets a weighted **attention score** (severe signals like
  "merged without a passing build" count for more) and a **NEEDS ATTENTION / ok**
  verdict, with the top reasons spelled out.
- A **people / team rollup** groups merged PRs by author and scores each — the
  closest proxy for "which teams". Drop a `teams.json` next to the script
  (`{"alice@corp.com": "Platform", ...}`) to roll up by team instead of person.
- Repos/people with fewer than `MIN_MERGED_FOR_ATTENTION` (default 5) merged PRs
  are shown as "few PRs" rather than flagged, to avoid noise on tiny samples.
- Set `HARNESS_SINCE_DAYS` to scope everything to recent activity (= "right now").

## Live-mode config (.env)

| Variable | Meaning |
|----------|---------|
| `HARNESS_API_KEY` | token for the host/account below *(required)* |
| `HARNESS_ACCOUNT_ID` | account id *(required)* |
| `HARNESS_BASE_URL` | host, e.g. `https://harness0.harness.io` (default `https://app.harness.io`) |
| `HARNESS_ORG` / `HARNESS_PROJECT` | only for project-scoped repos; omit for account-scoped |
| `HARNESS_REPO` | analyze just this one repo instead of all |
| `HARNESS_MAX_PAGES` | pages of 100 PRs to fetch (default 1 = first page; `0` = all history) |
| `HARNESS_SINCE_DAYS` | only PRs from the last N days |

## What behaviours correlate with higher-quality changes?

The final section answers the brief's third question by measuring **outcomes**, not
just practices:

- **Quality outcome** = a merged PR is "bad" if a later `Revert ... #NNNN` PR undid it.
- For each good behaviour (passing build, linked Jira, small change, unrushed merge)
  it compares the **revert rate of PRs that had it vs didn't**.
- A behaviour whose PRs are reverted *less* correlates with higher quality.

⚠️ Reverts are rare, so this needs history to be reliable. Run with
`HARNESS_MAX_PAGES=0` (all PRs). On small samples the percentages are noisy and
groups are imbalanced — treat single-digit revert counts as indicative only.
This is **correlation, not causation.**

## How "build passed" is decided

A PR's build **passes** only if it has at least one status check and *all*
checks succeeded. A merged PR with **no checks at all** counts as a violation —
because nothing gated the merge. That makes "we have no CI on this repo" show up
as a problem, which is exactly what the brief wants surfaced.

## Output

- A ranked table (worst repo first) by build-violation rate, with a health label.
- A per-signal breakdown across all four rules.
- A drill-down of the offending PRs in the repo that needs attention most.

## Deploy it as a live dashboard (Harness CI/CD)

Containerized and deployable to Kubernetes via Harness CD:
- `Dockerfile` — builds the web app
- `k8s/deployment.yaml` — Deployment + Service (LoadBalancer URL) + secret
- `harness/pipeline.yaml` — Harness CI (build image) + CD (deploy to K8s)
- **`DEPLOY.md`** — step-by-step runbook

Quick local container test: `docker build -t pr-health . && docker run -p 8080:8080 pr-health`.

## Extending it

Add a new signal by writing one function and a line in `evaluate_pr()`.
Everything else (aggregation, ranking, rendering) picks it up automatically.
