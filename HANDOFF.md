# PR Health Analyzer — Full Context Handoff

> **Why this document exists.** This is a complete description of a **working** PR-health
> application built on **Harness** (Harness Code as the SCM + Harness CI/CD + Slack for alerts +
> Claude for AI fix suggestions). It is being handed to the team/agent building a **sibling
> application with the identical problem statement but a different stack: GitHub as the SCM
> provider**, with an architecture that already does: GitHub PR created → trigger pipeline →
> `/analyse` API → store to DB → write a comment back to the GitHub PR → `/get` dashboard.
>
> The goal of the handoff is to let the GitHub app **absorb everything valuable here** — the
> domain logic (signals, scoring, correlation), the AI fix-suggester, the Slack alerting, and the
> build-failure flow — while swapping the Harness-specific I/O for GitHub equivalents.
>
> **The single most important idea:** all the *value* in this app is **SCM-agnostic domain logic**
> that operates on a small **normalized PR dict**. The Harness API is just one adapter that
> produces those dicts. The GitHub app already has a different adapter (its own ingestion + DB).
> So integration = *port the domain logic verbatim, feed it normalized PRs from your source.*

---

## 1. The problem statement (the bootcamp brief — identical for both apps)

Build a product that reads PR data and surfaces **engineering-quality insights**, answering:

1. **How healthy are our PR practices** across repos/teams?
2. **Which repositories/teams need attention right now?**
3. **What engineering behaviours correlate with higher-quality changes?**

The brief's hint: start with **ONE signal** — *"PRs merged without a passing build"* (the **MVP
signal**) — and rank repos by violation rate. Then layer on more signals.

---

## 2. What this app does (high level)

- Reads PRs + their **status checks** from Harness Code (or a bundled `sample_data.json`).
- Normalizes each PR into a common dict.
- Computes **4 health signals**, a per-repo **attention score**, a **people/team rollup**, and a
  **behaviour-vs-revert correlation**.
- Serves an HTML **dashboard** (3 sections = the 3 questions) with auto-refresh.
- Has a **live PR events feed** (driven by Harness repo webhooks).
- Offers an **AI "how do I fix this failing build?"** suggester (Claude, Bedrock-aware).
- Sends **Slack alerts** when a PR's build is seen failing.
- Can **write a status check back** to a PR commit (`post_check.py`) — analogous to your
  "write a comment back to GitHub" step.

It is intentionally **dependency-light**: Python stdlib `http.server`, `requests` for live reads,
`anthropic` for AI. **No database** — it reads on demand and keeps live events in memory. (Your
GitHub app already adds the DB layer; see §6.)

---

## 3. THE CORE DOMAIN LOGIC — port this verbatim (it is SCM-agnostic)

Everything in this section operates only on the **normalized PR dict** (§3.1). None of it knows or
cares whether the data came from Harness or GitHub. **This is the part to lift into the GitHub app.**

### 3.1 The normalized PR shape (the contract everything depends on)

```python
{
  "number": 123,
  "title": "feat: [PROJ-456] add retry logic",
  "description": "...",
  "source_branch": "feature/PROJ-456-retry",
  "target_branch": "main",
  "state": "merged",            # "open" | "closed" | "merged"
  "created_at": "2026-06-20T10:00:00Z",   # ISO 8601 (UTC)
  "merged_at":  "2026-06-20T10:40:00Z",   # ISO or None
  "additions": 120,
  "deletions": 30,
  "approvals": 0,               # number of review approvals
  "author_email": "alice@corp.com",
  "checks": [                   # status checks / CI runs on the head commit
    {"name": "ci/unit-tests", "status": "success", "link": "https://...", "summary": "..."},
    {"name": "ci/lint",       "status": "failure", "link": "https://...", "summary": "..."}
  ]
}
```

A repo is `{"name": "...", "pull_requests": [<normalized pr>, ...]}`. The whole pipeline consumes a
`list[repo]`.

> **GitHub mapping note:** GitHub gives you *real* review/approval data and *real* check-run
> conclusions — both are richer than what Harness exposed here. In particular **populate
> `approvals` properly** from GitHub reviews (this app hardcodes `approvals=0` because it never
> fetched Harness reviewers — a known limitation, see §9). See §5 for the full field mapping.

### 3.2 The 4 signals + exact thresholds

```python
FAST_MERGE_MINUTES = 10        # merged faster than this = "rushed"
LARGE_CHANGE_LINES = 400       # additions + deletions above this = "large"
MIN_REVIEWERS_FOR_LARGE = 1    # large changes should have >= this many approvals
JIRA_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")   # e.g. PROJ-123

SIGNALS = [
    "merged_without_passing_build",   # the MVP signal
    "no_linked_jira_ticket",
    "merged_too_fast",
    "large_change_low_review",
]
```

`evaluate_pr(pr)` returns `{signal: bool}` for one **merged** PR (returns `None` for non-merged):

```python
{
  "merged_without_passing_build": not _build_passed(pr),
  "no_linked_jira_ticket":        not _has_jira(pr),       # JIRA_KEY_RE over title+desc+branch
  "merged_too_fast":              minutes is not None and minutes < FAST_MERGE_MINUTES,
  "large_change_low_review":      (additions+deletions) > LARGE_CHANGE_LINES
                                    and approvals < MIN_REVIEWERS_FOR_LARGE,
}
```

### 3.3 Build-status semantics — the subtle, important part

This caused real bugs; get it right in the GitHub port.

```python
_HARD_FAIL_STATUSES = {"failure", "error", "failed"}      # only these count as a real failure
_RUNNING_STATUSES   = {"running", "pending", "queued", "scheduled"}

def _build_passed(pr):
    checks = pr.get("checks") or []
    if not checks:
        return False     # NO checks at all => nothing gated the merge => treat as a violation
    return not any(status in _HARD_FAIL_STATUSES for status in checks-statuses)

def build_state(pr):     # "none" | "failed" | "pending" | "passed"
    if no checks:        return "none"
    if any hard-fail:    return "failed"     # a real failure wins even if others still run
    if any running:      return "pending"    # don't claim green prematurely
    return "passed"

def failing_checks(pr):  # list of {name, link, summary} for HARD-failed checks only
    ...
```

Key rules learned the hard way:
- **`failure_ignored` is NOT a failure.** Harness has a status meaning "this check failed but was
  deliberately marked non-blocking." Counting it as a failure produced false "100% unhealthy"
  alarms. **Only `failure`/`error`/`failed` are hard failures.** *GitHub equivalent:* a check run's
  `conclusion` of `failure`/`timed_out`/`action_required` is hard; `neutral`/`skipped`/`success`
  are not. Map carefully.
- **No checks at all = violation, by design.** "This repo has no CI gating merges" is exactly what
  the brief wants surfaced, so an empty `checks` list means `_build_passed = False`.
- **Distinguish "no CI" (amber) from "failed" (red) from "pending" (blue)** in the UI — they're
  different states.

### 3.4 Attention scoring (Question 2)

```python
ATTENTION_WEIGHTS = {
    "merged_without_passing_build": 3,   # most serious — shipped unverified
    "large_change_low_review":      2,
    "no_linked_jira_ticket":        1,
    "merged_too_fast":              1,
}
ATTENTION_THRESHOLD     = 1.0    # flag when weighted-violations-per-merged-PR exceeds this
MIN_MERGED_FOR_ATTENTION = 5     # but only if the repo/person has >= 5 merged PRs (anti-noise)

attention_score = sum(signal_counts[s] * ATTENTION_WEIGHTS[s] for s in SIGNALS) / merged_count
needs_attention = merged_count >= MIN_MERGED_FOR_ATTENTION and attention_score >= ATTENTION_THRESHOLD
```

`analyze(repos)` produces per-repo summaries (merged count, MVP `build_violation_rate`,
`signal_counts`, `attention_score`, `needs_attention`, human-readable `reasons`, and `flagged_prs`
with their failing checks), sorted worst-first by build-violation rate.

`rollup_by_team(repos, team_map)` does the same scoring grouped by `author_email` (or by team if you
drop a `teams.json` = `{"alice@corp.com": "Platform", ...}`). This is the "which **teams** need
attention" proxy.

`health_label(rate)`: `>=30% POOR`, `>=15% FAIR`, `>0% GOOD`, `0% HEALTHY`, `None = no data`.

### 3.5 Correlation (Question 3)

Outcome proxy = **a merged PR is "bad" if a later PR titled `Revert ... #NNNN` undid it.**

```python
BEHAVIOURS = ["passing_build", "linked_jira", "small_change", "unrushed_merge"]
# find_reverted_numbers(): scan titles for "revert" + "#<n>" => set of reverted PR numbers
# correlate(): for each behaviour, compare revert-rate of PRs that HAD it vs DIDN'T.
# A behaviour whose PRs are reverted LESS correlates with higher quality. Correlation, not proof.
```

⚠️ Reverts are rare — this needs **history** (fetch all PRs, not just recent) to be meaningful.
*GitHub note:* GitHub's "Revert" button creates PRs titled `Revert "<original title>" (#N)` — the
same regex (`#(\d+)`) works.

---

## 4. Features layered on top of the core

### 4.1 AI fix suggester — `ai_suggest.py`

Given a PR's failing checks (+ optional log text), asks Claude for concrete fix steps. **One
Messages API call**, no agent loop.

- `suggest_fix(failing_checks, pr_title=None, log_text=None) -> str`
- Model: `claude-opus-4-8` (first-party id) / `anthropic.claude-opus-4-8` (Bedrock id).
- Uses `thinking={"type": "adaptive"}` and `max_tokens=4000`.
- **Auto-detects the credential type** in `_make_client()`:
  - key starts with `ABSK` **or** `AWS_BEARER_TOKEN_BEDROCK` set → **Amazon Bedrock**
    (`AnthropicBedrock(aws_region=...)`, needs `pip install 'anthropic[bedrock]'`, region from
    `BEDROCK_AWS_REGION` default `us-east-1`, model from `BEDROCK_MODEL`).
  - otherwise → first-party `anthropic.Anthropic()` (expects `sk-ant-...`).
- Degrades gracefully: missing key / missing package → returns a helpful string instead of crashing.
- System prompt = "senior CI/build engineer": (1) one line naming the most likely cause, then
  (2) numbered concrete fix steps; "Do NOT invent log lines."

**Reusable as-is in the GitHub app** — it's pure Claude, no Harness coupling. If you can pull the
**actual failing step log** from GitHub Actions (you can: `GET /repos/{o}/{r}/actions/jobs/{id}/logs`),
pass it as `log_text` for much better suggestions.

### 4.2 Slack notifier — `notify.py`

- Stdlib-only (`urllib`), no dependency. Posts to a **Slack Incoming Webhook**.
- `notify_build_failed(number, title, target, author, failing_checks, pr_url, suggest_url)` →
  posts a formatted 🚨 message with a "View PR" link and a "💡 AI fix suggestion" link.
- Enabled iff `SLACK_WEBHOOK_URL` is set; otherwise a graceful no-op (`slack_enabled()`).

**Reusable as-is** — only the link-building differs (point `pr_url` at the GitHub PR URL).

### 4.3 Live dashboard + events feed — in `pr_health.py serve()`

- `GET /` → the 3-section dashboard (auto-refresh via `<meta refresh=15>`).
- `GET /healthz` → `ok` (k8s probe).
- `GET /suggest?pr=N` → renders the AI fix suggestion page for PR N.
- `POST /webhook` → receives Harness repo webhooks; `parse_pr_event()` extracts
  `(action, number, target)`; `handle_event()` enriches the PR and pushes it onto an in-memory
  `events` list (most-recent-first, capped at 50). Always returns **200** so the sender keeps
  delivering.
- `POST /build-failed?pr=N` → see §4.4.
- On each `GET /`, the server **re-fetches** the current build status of feed PRs (checks often
  complete *after* the PR-opened webhook fired, so the snapshot would be stale).

### 4.4 Build-failure flow (Slack alert without polling) — the key integration pattern

**Problem:** SCM **repo** webhooks fire on PR/branch events, **not** on "a check finished." So you
can't learn "the build failed" from the same webhook that told you "a PR opened."

**Solution used here (recommended for the GitHub app too):** the **CI pipeline pushes** the failure.
- `harness/pr-check-pipeline.yaml` has a Run step `Notify Build Failed` with `when: stageStatus:
  Failure` that does:
  `curl -X POST "<dashboard_url>/build-failed?pr=<PR_NUMBER>"`.
- The server's `POST /build-failed?pr=N` enriches the PR, marks it failed (the pipeline is
  authoritative), adds it to the feed, and fires the **deduped** Slack alert (`maybe_notify()` sends
  at most one alert per PR number).

**GitHub equivalent:** in your GitHub Actions workflow, add a final step `if: failure()` that curls
your service's failure endpoint (or post straight to Slack). Because your app already has a DB +
`/analyse`, you can instead trigger the alert from inside `/analyse` whenever it computes
`build_state == "failed"` for a PR — even cleaner, and survives the server being restarted.

### 4.5 Writing back to the PR — `post_check.py`

Posts a status check onto a PR's head commit via Harness `PUT /repos/{repo}/checks/commits/{sha}`.
This is the mechanism external CI tools use to report status. **This is the direct analogue of your
GitHub app's "write a comment back to the PR" step** — except GitHub gives you two distinct
mechanisms: (a) **PR comments** (`POST /repos/{o}/{r}/issues/{n}/comments`) and (b) **Checks API**
(`POST /repos/{o}/{r}/check-runs`). Your app uses comments; you could *also* publish a real check.

---

## 5. Harness ↔ GitHub mapping (the translation table)

| Concept | This app (Harness) | GitHub equivalent |
|---|---|---|
| List PRs | `GET /code/api/v1/repos/{repo}/pullreq?state=open,closed,merged` | `GET /repos/{owner}/{repo}/pulls?state=all` |
| One PR | `GET .../pullreq/{number}` | `GET /repos/{o}/{r}/pulls/{number}` |
| PR is merged | `pr.merged` (ms timestamp, truthy) | `pull_request.merged` (bool) / `merged_at` set |
| Created/merged time | `created`,`merged` = **epoch ms** | `created_at`,`merged_at` = **ISO 8601 strings** |
| Diff size | `pr.stats.additions/deletions` | `pull.additions`/`pull.deletions` |
| Author | `pr.author.email/display_name` | `pull.user.login` (email via commits/API) |
| **Approvals** | *not fetched → hardcoded 0* ⚠️ | `GET .../pulls/{n}/reviews` → count `state == "APPROVED"` ✅ richer |
| **Status checks** | `GET .../checks/commits/{sha}` → items w/ `status` | `GET .../commits/{sha}/check-runs` (`conclusion`) + `/status` (legacy statuses) |
| Hard-fail status | `failure`/`error`/`failed` (NOT `failure_ignored`) | `conclusion` ∈ `failure`/`timed_out`/`action_required` (NOT `neutral`/`skipped`) |
| Webhook: PR events | repo webhook → `pullreq_created`/`merged`/... | GitHub webhook `pull_request` event (`opened`/`closed`+`merged`) |
| Webhook: build status | **not available on repo webhook** | `check_run`/`check_suite`/`status` webhook events ✅ (GitHub *can* push these!) |
| Write status back | `PUT .../checks/commits/{sha}` | `POST .../check-runs` or `POST .../issues/{n}/comments` |
| CI/CD | Harness CI (Cloud runners) + CD (K8s) | GitHub Actions (+ your deploy) |
| Scope/identity | account / org / project + repo slug | `owner` / `repo` |
| Auth | `x-api-key: pat....` header | `Authorization: Bearer <token>` (PAT / GitHub App installation token) |

**Two GitHub advantages to exploit:** (1) real **approvals/reviews** data — wire it into
`approvals` so `large_change_low_review` becomes meaningful; (2) GitHub **does** emit
`check_run`/`check_suite`/`status` webhooks — so unlike here, you may not need the pipeline-push or
polling workaround for build-failure alerts; you can react to a `check_run` `completed`+`failure`
webhook directly.

---

## 6. Architecture differences & integration plan

**This app (Harness):** stateless; reads on demand; live events kept **in memory**; renders HTML
server-side; no DB; analysis recomputed at startup / per request.

**Your app (GitHub):** event-driven with persistence — *PR created → trigger pipeline → `/analyse`
API → store to DB → comment back to GitHub → `/get` dashboard.* This is a **more robust**
architecture (survives restarts, has history).

**Recommended integration (best of both):**
1. **Lift §3 as a pure module** (call it `pr_health_core.py`): the signals, thresholds, build
   semantics, `evaluate_pr`, `analyze`, `rollup_by_team`, `correlate`, `health_label`. Zero I/O.
   It only needs the normalized PR dict (§3.1).
2. In your **GitHub ingestion adapter**, map GitHub PR + reviews + check-runs → the normalized dict
   (use §5). Crucially, **populate `approvals` for real.**
3. Call the core from your **`/analyse`** endpoint; **persist** the resulting per-repo/per-team
   summaries + flagged PRs to your **DB** (this app never did — your DB makes trends-over-time and
   the `/get` dashboard much better).
4. Your **comment write-back**: format the offending signals + (optionally) the **AI fix
   suggestion** (`ai_suggest.suggest_fix`) into the PR comment. That fuses two features.
5. **Slack** (`notify.py`) and the **AI suggester** (`ai_suggest.py`) drop in unchanged — they're
   already SCM-agnostic. Trigger the Slack alert from `/analyse` when `build_state == "failed"`.
6. For **build-failure detection**, prefer GitHub's `check_run`/`check_suite` webhook (no polling,
   no pipeline-push hack needed) → call `/analyse` for that PR → alert + comment.

---

## 7. File inventory

| File | Role |
|---|---|
| `pr_health.py` | **Core.** Signals, scoring, correlation, normalization, live Harness reads (`load_live`/`enrich_pr`), the web server (`serve`), webhook + `/build-failed` handlers, HTML dashboard (`build_html`). ~1000 lines. |
| `ai_suggest.py` | Claude-based "how to fix this failing build" suggester. Bedrock/first-party auto-detect. |
| `notify.py` | Slack Incoming Webhook notifier (`notify_build_failed`). Stdlib only. |
| `post_check.py` | Posts a status check back to a PR commit (Harness `PUT .../checks`). Analogue of GitHub comment/check write-back. |
| `sample_data.json` | 3 demo repos (payments-api, data-pipeline, web-frontend) — lets everything run with no network. |
| `Dockerfile` | `python:3.11-slim` + `requests`,`anthropic`; serves on 8080; uses `--live` if a token is present. |
| `k8s/deployment.yaml` | Namespace + Secret + Deployment (`/healthz` probes) + LoadBalancer Service. |
| `harness/pipeline.yaml` | Harness CI (BuildAndPushDockerRegistry) + CD (K8sRollingDeploy) to deploy the dashboard. |
| `harness/pr-check-pipeline.yaml` | Minimal CI pipeline that runs PR checks + the `Notify Build Failed` failure step (`curl /build-failed`). |
| `README.md` / `DEPLOY.md` | Usage + deploy runbook. |
| `.env` / `.env.example` | Config (git-ignored secrets). |

**Run modes:** `python3 pr_health.py` (sample), `--json`, `--html report.html`, `--serve`
(web on :8080), add `--live` to read Harness.

---

## 8. Config / secrets (env vars)

| Variable | Meaning |
|---|---|
| `HARNESS_API_KEY`, `HARNESS_ACCOUNT_ID` | Harness auth (required for live). *GitHub: replace with `GITHUB_TOKEN`/App creds + `owner`/`repo`.* |
| `HARNESS_BASE_URL`, `HARNESS_ORG`, `HARNESS_PROJECT`, `HARNESS_REPO` | Harness scope/host. |
| `HARNESS_MAX_PAGES` (0=all), `HARNESS_SINCE_DAYS` | History depth / recency window. |
| `ANTHROPIC_API_KEY` | Claude key. **`ABSK...` ⇒ Bedrock auto-detected**; `sk-ant-...` ⇒ first-party. |
| `BEDROCK_AWS_REGION` (def `us-east-1`), `BEDROCK_MODEL` (def `anthropic.claude-opus-4-8`) | Bedrock tuning. |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook (blank = alerts off). |
| `DASHBOARD_URL` | Public URL used to build the "AI fix" link inside Slack alerts. |

`load_dotenv()` is a tiny custom `.env` reader (strips inline comments; doesn't override real env).

---

## 9. Gotchas & lessons learned (save the GitHub app from repeating these)

1. **`failure_ignored` ≠ failure.** Only `failure`/`error`/`failed` are hard fails. Mis-handling
   this produced false "100% unhealthy" results. (GitHub: be equally careful with
   `neutral`/`skipped` check conclusions.)
2. **No-CI = violation by design.** Empty `checks` ⇒ `_build_passed=False`. Intentional — surfaces
   ungated repos.
3. **Build status lags the PR-open webhook.** Checks finish *after* the PR opens, so a point-in-time
   webhook payload is stale; you must re-fetch (this app) or react to a dedicated check webhook
   (GitHub's advantage).
4. **Repo webhooks don't carry build status** → had to push from the CI pipeline (`/build-failed`)
   or poll. GitHub's `check_run` webhook removes this constraint.
5. **`approvals` is hardcoded to 0 here** (Harness reviewers were never fetched), so
   `large_change_low_review` effectively fires on any large change. **GitHub has real review data —
   fix this** for a genuinely better signal.
6. **Timestamps differ:** Harness = epoch **ms**; GitHub = **ISO strings**. The normalized shape
   uses ISO; convert at the adapter boundary.
7. **Pending vs passed:** don't render green while checks are still running — track a `pending`
   state.
8. **Webhook receiver must always return 200**, log the raw body, and tolerate unknown event
   shapes — otherwise the sender disables the webhook.
9. **Dedupe alerts** (one Slack message per PR) — `maybe_notify()` keeps a `notified` set.
10. **(Harness-specific, ignore for GitHub):** in this environment, Harness **MCP writes were
    org-policy-blocked**; reads worked. Not relevant to a GitHub PAT.
11. **Secrets hygiene:** `.env` is git-ignored; the Bedrock key and Slack URL are real secrets — the
    GitHub app should use its platform's secret store (GitHub Actions secrets / k8s Secret), never
    commit them.

---

## 10. TL;DR for the GitHub app's builder

- The **brain** is §3 — copy it into a pure module; it already works.
- Write a **GitHub adapter** that emits the §3.1 normalized dict (use the §5 table; **populate
  `approvals` for real**).
- Call the brain from your **`/analyse`**, persist to your **DB**, and enrich your **PR comment** +
  **Slack alert** with the **AI fix suggestion** (`ai_suggest.py`) and the failing-check details.
- `ai_suggest.py` and `notify.py` are **drop-in** (SCM-agnostic).
- Use GitHub's **`check_run` webhook** for build-failure alerts instead of the pipeline-push/poll
  workaround this app needed.
