#!/usr/bin/env python3
"""
PR Health Analyzer
==================

Reads pull-request data (from Harness SCM, or a local sample file) and surfaces
unhealthy engineering practices, ranking repositories by how often they violate
PR-quality rules.

Signals checked (from the brief):
  1. merged_without_passing_build  -- the MVP signal
  2. no_linked_jira_ticket
  3. merged_too_fast               -- rushed, likely unreviewed
  4. large_change_low_review       -- big diff, no/low reviewer participation

Usage:
  python pr_health.py                      # uses the bundled sample_data.json
  python pr_health.py --data sample_data.json
  python pr_health.py --live               # reads live data from Harness (needs env vars)
  python pr_health.py --json               # machine-readable output

Live mode environment variables:
  HARNESS_API_KEY      (required)
  HARNESS_ACCOUNT_ID   (required)
  HARNESS_ORG          (default: default)
  HARNESS_PROJECT      (required)
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

# ----------------------------------------------------------------------------
# Tunable thresholds for what counts as a "violation"
# ----------------------------------------------------------------------------
FAST_MERGE_MINUTES = 10        # merged faster than this = "rushed"
LARGE_CHANGE_LINES = 400       # additions + deletions above this = "large"
MIN_REVIEWERS_FOR_LARGE = 1    # large changes should have at least this many approvals

JIRA_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")  # e.g. PROJ-123

SIGNALS = [
    "merged_without_passing_build",
    "no_linked_jira_ticket",
    "merged_too_fast",
    "large_change_low_review",
]

# How serious each violation is when scoring which repos/teams need attention.
ATTENTION_WEIGHTS = {
    "merged_without_passing_build": 3,   # most serious -- shipped unverified
    "large_change_low_review": 2,        # big change nobody reviewed
    "no_linked_jira_ticket": 1,          # untraceable change
    "merged_too_fast": 1,                # rushed
}
# A repo/team is flagged when its weighted attention score per merged PR exceeds this.
ATTENTION_THRESHOLD = 1.0
# ...but only if it has at least this many merged PRs (avoids flagging on 1-2 PRs).
MIN_MERGED_FOR_ATTENTION = 5


# ----------------------------------------------------------------------------
# Signal detection -- operates on a single normalized PR dict
# ----------------------------------------------------------------------------
# Statuses that count as a genuine, blocking build failure. Note "failure_ignored"
# is NOT here: it's a check the team deliberately marked non-blocking, so merging
# past it is not a violation. "skipped"/"neutral"/"pending" are also not failures.
_HARD_FAIL_STATUSES = {"failure", "error", "failed"}


def _build_passed(pr):
    """A build 'passes' unless a check HARD-failed. No checks at all = not gated = fail."""
    checks = pr.get("checks") or []
    if not checks:
        return False  # no checks ran at all -> nothing gated the merge
    return not any(str(c.get("status", "")).lower() in _HARD_FAIL_STATUSES for c in checks)


_RUNNING_STATUSES = {"running", "pending", "queued", "scheduled"}


def build_state(pr):
    """States: 'none' (no CI), 'failed' (a hard failure), 'pending' (still running),
    'passed' (all checks finished and none hard-failed)."""
    checks = pr.get("checks") or []
    if not checks:
        return "none"
    statuses = [str(c.get("status", "")).lower() for c in checks]
    if any(s in _HARD_FAIL_STATUSES for s in statuses):
        return "failed"          # a real failure wins, even if others still run
    if any(s in _RUNNING_STATUSES for s in statuses):
        return "pending"         # not done yet -> don't claim green prematurely
    return "passed"


def failing_checks(pr):
    """Checks that HARD-failed — {name, link}. Excludes failure_ignored/skipped/etc."""
    out = []
    for c in (pr.get("checks") or []):
        if str(c.get("status", "")).lower() in _HARD_FAIL_STATUSES:
            out.append({"name": c.get("name") or "(unnamed check)",
                        "link": c.get("link"), "summary": c.get("summary")})
    return out


def _has_jira(pr):
    text = f"{pr.get('title', '')} {pr.get('description', '')} {pr.get('source_branch', '')}"
    return bool(JIRA_KEY_RE.search(text))


def _merge_minutes(pr):
    created, merged = pr.get("created_at"), pr.get("merged_at")
    if not created or not merged:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        delta = datetime.strptime(merged, fmt) - datetime.strptime(created, fmt)
        return delta.total_seconds() / 60.0
    except ValueError:
        return None


def evaluate_pr(pr):
    """Return {signal: bool} for one merged PR. Only merged PRs are evaluated."""
    if pr.get("state") != "merged":
        return None

    minutes = _merge_minutes(pr)
    size = (pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0)
    approvals = pr.get("approvals", 0) or 0

    return {
        "merged_without_passing_build": not _build_passed(pr),
        "no_linked_jira_ticket": not _has_jira(pr),
        "merged_too_fast": minutes is not None and minutes < FAST_MERGE_MINUTES,
        "large_change_low_review": size > LARGE_CHANGE_LINES
        and approvals < MIN_REVIEWERS_FOR_LARGE,
    }


# ----------------------------------------------------------------------------
# Question 3: which engineering behaviours correlate with higher-quality changes?
# Outcome proxy = a change that later got reverted is a "bad" change.
# ----------------------------------------------------------------------------
REVERT_RE = re.compile(r"#(\d+)")

# The positive behaviours we test against the revert outcome.
BEHAVIOURS = ["passing_build", "linked_jira", "small_change", "unrushed_merge"]


def pr_behaviours(pr):
    """Return {behaviour: bool} -- the GOOD practices present on this PR."""
    minutes = _merge_minutes(pr)
    size = (pr.get("additions", 0) or 0) + (pr.get("deletions", 0) or 0)
    return {
        "passing_build": _build_passed(pr),
        "linked_jira": _has_jira(pr),
        "small_change": size <= LARGE_CHANGE_LINES,
        "unrushed_merge": minutes is None or minutes >= FAST_MERGE_MINUTES,
    }


def find_reverted_numbers(prs):
    """PR numbers that a later 'Revert ... #NNNN' PR undid = changes that went bad."""
    reverted = set()
    for pr in prs:
        title = (pr.get("title") or "")
        if "revert" in title.lower():
            for num in REVERT_RE.findall(title):
                reverted.add(int(num))
    return reverted


def correlate(repos):
    """For each behaviour, compare the revert rate of PRs that HAD it vs DID NOT."""
    stats = {b: {"with_total": 0, "with_bad": 0, "wo_total": 0, "wo_bad": 0}
             for b in BEHAVIOURS}
    total_merged = total_reverted = 0

    for repo in repos:
        prs = repo.get("pull_requests", [])
        reverted = find_reverted_numbers(prs)
        for pr in prs:
            if pr.get("state") != "merged":
                continue
            total_merged += 1
            bad = pr.get("number") in reverted
            total_reverted += bad
            for name, present in pr_behaviours(pr).items():
                side = "with" if present else "wo"
                stats[name][f"{side}_total"] += 1
                if bad:
                    stats[name][f"{side}_bad"] += 1

    rows = []
    for b in BEHAVIOURS:
        s = stats[b]
        with_rate = (s["with_bad"] / s["with_total"] * 100) if s["with_total"] else None
        wo_rate = (s["wo_bad"] / s["wo_total"] * 100) if s["wo_total"] else None
        rows.append({
            "behaviour": b,
            "with_total": s["with_total"], "with_rate": with_rate,
            "wo_total": s["wo_total"], "wo_rate": wo_rate,
        })
    return {"behaviours": rows, "merged": total_merged, "reverted": total_reverted}


# ----------------------------------------------------------------------------
# Aggregation per repository
# ----------------------------------------------------------------------------
def analyze(repos):
    """repos: list of {name, pull_requests:[...]}.  Returns a list of repo summaries."""
    summaries = []
    for repo in repos:
        prs = repo.get("pull_requests", [])
        merged = [p for p in prs if p.get("state") == "merged"]
        signal_counts = {s: 0 for s in SIGNALS}
        flagged_prs = []

        for pr in merged:
            result = evaluate_pr(pr)
            hits = [s for s, bad in result.items() if bad]
            for s in hits:
                signal_counts[s] += 1
            if hits:
                flagged_prs.append({"number": pr.get("number"),
                                    "title": pr.get("title"),
                                    "violations": hits,
                                    "failing_checks": failing_checks(pr)})

        merged_count = len(merged)
        # MVP headline metric: % of merged PRs that merged without a passing build
        no_build = signal_counts["merged_without_passing_build"]
        build_violation_rate = (no_build / merged_count * 100) if merged_count else None

        # Composite: total violations across all signals, normalized
        total_violations = sum(signal_counts.values())
        composite_rate = (
            total_violations / (merged_count * len(SIGNALS)) * 100
            if merged_count else None
        )

        # Attention score: weighted violations per merged PR (higher = worse).
        weighted = sum(signal_counts[s] * ATTENTION_WEIGHTS[s] for s in SIGNALS)
        attention_score = (weighted / merged_count) if merged_count else 0.0

        # Human-readable reasons, worst signal first.
        reasons = []
        for s in sorted(SIGNALS, key=lambda x: -signal_counts[x]):
            if signal_counts[s] and merged_count:
                pct = signal_counts[s] / merged_count * 100
                reasons.append(f"{signal_counts[s]} {s.replace('_', ' ')} ({pct:.0f}%)")

        summaries.append({
            "repo": repo.get("name"),
            "merged_prs": merged_count,
            "open_prs": len([p for p in prs if p.get("state") == "open"]),
            "no_build_count": no_build,
            "build_violation_rate": build_violation_rate,
            "signal_counts": signal_counts,
            "composite_rate": composite_rate,
            "attention_score": attention_score,
            "needs_attention": merged_count >= MIN_MERGED_FOR_ATTENTION
            and attention_score >= ATTENTION_THRESHOLD,
            "reasons": reasons,
            "flagged_prs": flagged_prs,
        })

    # Rank worst-first by the MVP metric; repos with no data sink to the bottom
    summaries.sort(
        key=lambda s: (s["build_violation_rate"] is None,
                       -(s["build_violation_rate"] or 0)),
    )
    return summaries


def health_label(rate):
    if rate is None:
        return "no data"
    if rate >= 30:
        return "POOR"
    if rate >= 15:
        return "FAIR"
    if rate > 0:
        return "GOOD"
    return "HEALTHY"


def rollup_by_team(repos, team_map=None):
    """Group merged PRs by author (or by team, if a {author_email: team} map is given)
    and score each group. Approximates 'which teams need attention'."""
    groups = {}
    for repo in repos:
        for pr in repo.get("pull_requests", []):
            if pr.get("state") != "merged":
                continue
            who = pr.get("author_email") or pr.get("author") or "unknown"
            key = (team_map or {}).get(who, who)
            g = groups.setdefault(key, {"name": key, "merged": 0, "weighted": 0,
                                        "signal_counts": {s: 0 for s in SIGNALS}})
            g["merged"] += 1
            for s, bad in (evaluate_pr(pr) or {}).items():
                if bad:
                    g["signal_counts"][s] += 1
                    g["weighted"] += ATTENTION_WEIGHTS[s]
    rows = []
    for g in groups.values():
        score = g["weighted"] / g["merged"] if g["merged"] else 0.0
        rows.append({**g, "attention_score": score,
                     "needs_attention": g["merged"] >= MIN_MERGED_FOR_ATTENTION
                     and score >= ATTENTION_THRESHOLD})
    # Eligible authors (enough PRs to judge) first, then by score -- so the people
    # who actually need attention rank above tiny-sample outliers.
    rows.sort(key=lambda r: (r["merged"] < MIN_MERGED_FOR_ATTENTION, -r["attention_score"]))
    return rows


def render_correlation(corr):
    """Answers: which engineering behaviours correlate with higher-quality changes?"""
    print("\n" + "=" * 78)
    print("WHAT BEHAVIOURS CORRELATE WITH HIGHER-QUALITY CHANGES?")
    print("=" * 78)
    print(f"Outcome = whether a merged PR was later reverted "
          f"({corr['reverted']} reverts among {corr['merged']} merged PRs).")

    if corr["reverted"] == 0:
        print("\nNo reverts detected in this window -- can't measure outcomes yet.")
        print("Fetch more history (HARNESS_MAX_PAGES=0) so reverts appear.\n")
        return

    print("-" * 78)
    print(f"{'Behaviour':<20}{'revert% WITH':>16}{'revert% WITHOUT':>18}{'Effect':>22}")
    print("-" * 78)
    for r in corr["behaviours"]:
        w = f"{r['with_rate']:.1f}% (n={r['with_total']})" if r["with_rate"] is not None else "n/a"
        o = f"{r['wo_rate']:.1f}% (n={r['wo_total']})" if r["wo_rate"] is not None else "n/a"
        effect = "insufficient data"
        if r["with_rate"] is not None and r["wo_rate"] is not None:
            diff = r["wo_rate"] - r["with_rate"]
            if diff > 1.0:
                effect = "correlates w/ quality"
            elif diff < -1.0:
                effect = "correlates w/ RISK"
            else:
                effect = "little effect"
        print(f"{r['behaviour'].replace('_', ' '):<20}{w:>16}{o:>18}{effect:>22}")
    print("-" * 78)
    print("Read: lower revert% WITH a behaviour than WITHOUT it = that practice")
    print("correlates with higher-quality (less-reverted) changes. Correlation, not proof.\n")


def _verdict(needs_attention, merged):
    if needs_attention:
        return "NEEDS ATTENTION"
    if merged < MIN_MERGED_FOR_ATTENTION:
        return "few PRs"
    return "ok"


def render_attention(summaries, teams):
    """Answers: which repositories or teams need attention right now?"""
    print("\n" + "=" * 78)
    print("WHICH REPOSITORIES OR TEAMS NEED ATTENTION RIGHT NOW?")
    print("=" * 78)

    repos_sorted = sorted(summaries, key=lambda s: -s["attention_score"])
    flagged = [s for s in repos_sorted if s["needs_attention"]]

    print("\nRepositories (ranked by attention score; higher = worse):")
    print("-" * 78)
    print(f"{'Repo':<24}{'Merged':>7}{'Score':>8}{'Verdict':>18}")
    print("-" * 78)
    for s in repos_sorted:
        verdict = _verdict(s["needs_attention"], s["merged_prs"])
        print(f"{s['repo']:<24}{s['merged_prs']:>7}{s['attention_score']:>8.2f}{verdict:>18}")
    print("-" * 78)

    if flagged:
        print("\nWhy these repos need attention:")
        for s in flagged:
            print(f"  {s['repo']}:  " + "; ".join(s["reasons"][:3]))
    else:
        print("\nNo repository crosses the attention threshold right now.")

    # Team / author rollup
    print("\nPeople / teams (ranked by attention score):")
    print("-" * 78)
    print(f"{'Author / team':<34}{'Merged':>7}{'Score':>8}{'Verdict':>18}")
    print("-" * 78)
    for r in teams[:10]:
        verdict = _verdict(r["needs_attention"], r["merged"])
        print(f"{(r['name'] or 'unknown')[:32]:<34}{r['merged']:>7}"
              f"{r['attention_score']:>8.2f}{verdict:>18}")
    print("-" * 78)
    print("(Tip: set HARNESS_SINCE_DAYS in .env to scope this to recent activity = "
          "'right now'.)")
    print()


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------
def render_table(summaries):
    print()
    print("PR HEALTH  -  repositories ranked by build-violation rate (worst first)")
    print("=" * 78)
    print(f"{'Repo':<22}{'Merged':>7}{'No-build':>10}{'Violation%':>12}{'Health':>12}")
    print("-" * 78)
    for s in summaries:
        rate = s["build_violation_rate"]
        rate_str = f"{rate:.1f}%" if rate is not None else "n/a"
        print(f"{s['repo']:<22}{s['merged_prs']:>7}{s['no_build_count']:>10}"
              f"{rate_str:>12}{health_label(rate):>12}")
    print("-" * 78)

    print("\nAll signals (count of merged PRs violating each rule):")
    print("-" * 78)
    header = f"{'Repo':<22}" + "".join(f"{s.split('_')[0][:8]:>11}" for s in SIGNALS)
    print(f"{'Repo':<22}{'no-build':>11}{'no-jira':>11}{'too-fast':>11}{'big/low-rev':>13}")
    print("-" * 78)
    for s in summaries:
        c = s["signal_counts"]
        print(f"{s['repo']:<22}{c['merged_without_passing_build']:>11}"
              f"{c['no_linked_jira_ticket']:>11}{c['merged_too_fast']:>11}"
              f"{c['large_change_low_review']:>13}")
    print("-" * 78)

    # Drill-down: the single worst repo's offending PRs
    worst = next((s for s in summaries if s["flagged_prs"]), None)
    if worst:
        total = len(worst["flagged_prs"])
        shown = worst["flagged_prs"][:100]
        print(f"\nNeeds attention now:  {worst['repo']}  "
              f"(showing {len(shown)} of {total} offending PRs)")
        print("-" * 78)
        for pr in shown:
            vio = ", ".join(v.replace("_", " ") for v in pr["violations"])
            print(f"  PR #{pr['number']}  {(pr['title'] or '')[:40]:<40}  -> {vio}")
            if pr.get("failing_checks"):
                names = ", ".join(c["name"] for c in pr["failing_checks"])
                print(f"             failed checks: {names}")
    print()


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------
def load_sample(path):
    with open(path) as f:
        return json.load(f)["repositories"]


def load_dotenv(path=".env"):
    """Minimal .env loader (no dependency). Lines like KEY=value; # comments ignored.
    Does not overwrite variables already set in the real environment."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            # Strip an inline comment (e.g. `HARNESS_SINCE_DAYS=1   # note`),
            # unless the value is quoted (tokens/URLs here never contain '#').
            if value[:1] not in ('"', "'") and "#" in value:
                value = value.split("#", 1)[0]
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def as_list(resp):
    """Harness sometimes returns a bare list, sometimes {content|data|checks:[...]}."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for key in ("content", "data", "checks", "pull_requests"):
            if isinstance(resp.get(key), list):
                return resp[key]
    return []


def check_status(item):
    """A check item may carry status at the top level or nested under 'check'."""
    if not isinstance(item, dict):
        return None
    if item.get("status"):
        return item["status"]
    nested = item.get("check")
    return nested.get("status") if isinstance(nested, dict) else None


def check_name(item):
    """The check's name (identifier), top-level or nested under 'check'."""
    if not isinstance(item, dict):
        return None
    nested = item.get("check") if isinstance(item.get("check"), dict) else {}
    return item.get("identifier") or nested.get("identifier") or item.get("name")


def _normalize_checks(raw):
    return [{"name": check_name(c), "status": check_status(c),
             "link": (c.get("link") if isinstance(c, dict) else None),
             "summary": (c.get("summary") if isinstance(c, dict) else None)}
            for c in as_list(raw)]


def _normalize_pr(pr, checks):
    """Map a raw Harness Code PR + checks into the analyzer's common shape."""
    author = pr.get("author") or {}
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "description": pr.get("description"),
        "source_branch": pr.get("source_branch"),
        "target_branch": pr.get("target_branch"),
        "state": "merged" if pr.get("merged") else pr.get("state"),
        "created_at": _ms_to_iso(pr.get("created")),
        "merged_at": _ms_to_iso(pr.get("merged")),
        "additions": (pr.get("stats") or {}).get("additions", 0),
        "deletions": (pr.get("stats") or {}).get("deletions", 0),
        "approvals": 0,
        "author_email": author.get("email") or author.get("display_name"),
        "checks": checks,
    }


def harness_config():
    """Read Harness connection config from env. Returns (base_url, headers, scope)."""
    api_key = os.environ.get("HARNESS_API_KEY")
    account = os.environ.get("HARNESS_ACCOUNT_ID")
    if not (api_key and account):
        raise RuntimeError("Need HARNESS_API_KEY and HARNESS_ACCOUNT_ID")
    host = os.environ.get("HARNESS_BASE_URL", "https://app.harness.io").rstrip("/")
    scope = {"accountIdentifier": account}
    if os.environ.get("HARNESS_ORG"):
        scope["orgIdentifier"] = os.environ["HARNESS_ORG"]
    if os.environ.get("HARNESS_PROJECT"):
        scope["projectIdentifier"] = os.environ["HARNESS_PROJECT"]
    return f"{host}/code/api/v1", {"x-api-key": api_key}, scope


def pr_web_url(number):
    """Best-effort Harness Code UI URL for a PR, for linking in notifications."""
    host = os.environ.get("HARNESS_BASE_URL", "https://app.harness.io").rstrip("/")
    account = os.environ.get("HARNESS_ACCOUNT_ID")
    org = os.environ.get("HARNESS_ORG")
    project = os.environ.get("HARNESS_PROJECT")
    repo = os.environ.get("HARNESS_REPO")
    if not (account and repo and number is not None):
        return None
    scope_path = f"orgs/{org}/projects/{project}/" if org and project else ""
    return (f"{host}/ng/account/{account}/module/code/{scope_path}"
            f"repos/{repo}/pulls/{number}/conversation")


def enrich_pr(number):
    """Fetch ONE pull request's full details + checks on demand (for the webhook)."""
    import requests
    base, headers, scope = harness_config()
    repo = os.environ.get("HARNESS_REPO")

    def get(path):
        r = requests.get(f"{base}/{path}", headers=headers, params=scope, timeout=30)
        r.raise_for_status()
        return r.json()

    raw = get(f"repos/{repo}/pullreq/{number}")
    sha = raw.get("source_sha") or raw.get("merge_base_sha") or ""
    checks = _normalize_checks(get(f"repos/{repo}/checks/commits/{sha}")) if sha else []
    return _normalize_pr(raw, checks)


def load_live():
    """Fetch repos + PRs + checks from Harness Code REST API.

    Paginates through ALL pull requests (not just the first page), so PRs from a
    year ago are included. Honors an optional date window via HARNESS_SINCE_DAYS.

    Config (env / .env):
      HARNESS_API_KEY     (required)  token for the SAME host/account below
      HARNESS_ACCOUNT_ID  (required)
      HARNESS_BASE_URL    host, default https://app.harness.io
      HARNESS_ORG         optional (omit for account-scoped repos)
      HARNESS_PROJECT     optional (omit for account-scoped repos)
      HARNESS_REPO        optional; analyze just this repo instead of all of them
      HARNESS_SINCE_DAYS  optional; only PRs created within the last N days
    """
    try:
        import requests
    except ImportError:
        sys.exit("Live mode needs the 'requests' package:  pip install requests")

    api_key = os.environ.get("HARNESS_API_KEY")
    account = os.environ.get("HARNESS_ACCOUNT_ID")
    if not (api_key and account):
        sys.exit("Live mode needs HARNESS_API_KEY and HARNESS_ACCOUNT_ID")

    host = os.environ.get("HARNESS_BASE_URL", "https://app.harness.io").rstrip("/")
    base = f"{host}/code/api/v1"
    headers = {"x-api-key": api_key}

    # Scope: org/project are optional. Omit them for account-scoped repos.
    scope = {"accountIdentifier": account}
    if os.environ.get("HARNESS_ORG"):
        scope["orgIdentifier"] = os.environ["HARNESS_ORG"]
    if os.environ.get("HARNESS_PROJECT"):
        scope["projectIdentifier"] = os.environ["HARNESS_PROJECT"]

    only_repo = os.environ.get("HARNESS_REPO")
    # Default: first page only (100 PRs), like the original behavior.
    # Set HARNESS_MAX_PAGES higher (or 0 for unlimited) to fetch older PRs.
    max_pages = int(os.environ.get("HARNESS_MAX_PAGES", "1"))
    since_days = os.environ.get("HARNESS_SINCE_DAYS")
    cutoff_ms = None
    if since_days:
        import time
        cutoff_ms = (time.time() - float(since_days) * 86400) * 1000

    def get(url, params):
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code >= 400:
            sys.exit(f"Harness API {r.status_code} on {url}\n{r.text[:400]}")
        return r.json()

    # Which repos to analyze
    if only_repo:
        repo_idents = [only_repo]
    else:
        repos = as_list(get(f"{base}/repos", scope))
        repo_idents = [r.get("identifier") or r.get("uid") for r in repos]

    result = []
    for ident in repo_idents:
        # Paginate through ALL pull requests, newest first.
        all_prs, page = [], 1
        while True:
            batch = as_list(get(
                f"{base}/repos/{ident}/pullreq",
                {**scope, "state": ["open", "closed", "merged"], "limit": 100, "page": page},
            ))
            if not batch:
                break
            all_prs.extend(batch)
            if cutoff_ms and all(p.get("created", 0) < cutoff_ms for p in batch):
                break  # whole page older than the window -> stop paging
            if len(batch) < 100:
                break
            if max_pages and page >= max_pages:
                break  # default max_pages=1 -> first page only
            page += 1

        normalized = []
        for pr in all_prs:
            if cutoff_ms and (pr.get("created") or 0) < cutoff_ms:
                continue
            sha = pr.get("source_sha") or pr.get("merge_base_sha") or ""
            checks = (_normalize_checks(get(f"{base}/repos/{ident}/checks/commits/{sha}", scope))
                      if sha else [])
            normalized.append(_normalize_pr(pr, checks))
        result.append({"name": ident, "pull_requests": normalized})
    return result


def _ms_to_iso(ms):
    if not ms:
        return None
    return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# HTML dashboard (for the deployed web view)
# ----------------------------------------------------------------------------
def _badge(rate):
    label = health_label(rate)
    color = {"POOR": "#e5484d", "FAIR": "#f5a524", "GOOD": "#30a46c",
             "HEALTHY": "#30a46c", "no data": "#8b8b8b"}[label]
    return f'<span class="badge" style="background:{color}">{label}</span>'


def _bar(pct):
    pct = max(0, min(100, pct or 0))
    return (f'<div class="bar"><div class="fill" style="width:{pct:.0f}%"></div>'
            f'<span>{pct:.0f}%</span></div>')


def _events_html(events):
    if not events:
        return ("<section><h2>🔔 Live PR events (webhook)</h2>"
                "<p class='sub'>Waiting for events… open or merge a PR to main/master.</p></section>")
    items = ""
    for e in events[:25]:
        acolor = "#9ecbff" if e["action"] == "opened" else "#8957e5"
        bcolor, bstate = {
            "passed": ("#30a46c", "build ✓"),
            "failed": ("#e5484d", "build ✗ failed"),
            "pending": ("#3b82f6", "build running…"),
            "none": ("#f5a524", "no CI checks"),
            "unknown": ("#8b8b8b", "build n/a"),
        }.get(e.get("build_state", "unknown"), ("#8b8b8b", "build n/a"))
        chk = ""
        if e.get("failing_checks"):
            links = ", ".join(
                (f'<a href="{c["link"]}" target="_blank">{c["name"]}</a>'
                 if c.get("link") else c["name"]) for c in e["failing_checks"])
            chk = f"<div class='checks'>failed checks: {links}</div>"
        if e.get("build_state") == "failed" and e.get("number") is not None:
            chk += (f"<div class='checks'>💡 <a href='/suggest?pr={e['number']}'>"
                    f"ask AI how to fix this build</a></div>")
        num = f"#{e['number']} " if e.get("number") is not None else ""
        items += (
            f"<li><span class='badge' style='background:{acolor}'>{e['action']}</span> "
            f"<b>{num}</b>{(e.get('title') or '')[:70]} "
            f"<span class='sub'>→ {e.get('target') or '?'} · {e.get('author') or ''} · {e['time']}</span> "
            f"<span class='badge' style='background:{bcolor}'>{bstate}</span>{chk}</li>")
    return f"<section><h2>🔔 Live PR events (webhook)</h2><ul class='flagged'>{items}</ul></section>"


def build_html(summaries, teams, corr, generated_at, events=None):
    events_html = _events_html(events)
    rows = "".join(
        f"<tr><td>{s['repo']}</td><td>{s['merged_prs']}</td>"
        f"<td>{s['no_build_count']}</td>"
        f"<td>{_bar(s['build_violation_rate'])}</td>"
        f"<td>{_badge(s['build_violation_rate'])}</td></tr>"
        for s in summaries)

    attn = "".join(
        f"<li><b>{s['repo']}</b> — score {s['attention_score']:.2f}"
        f"<ul>{''.join(f'<li>{r}</li>' for r in s['reasons'][:3])}</ul></li>"
        for s in sorted(summaries, key=lambda x: -x['attention_score'])
        if s['needs_attention']) or "<li>No repository crosses the attention threshold.</li>"

    team_rows = "".join(
        f"<tr><td>{(r['name'] or 'unknown')}</td><td>{r['merged']}</td>"
        f"<td>{r['attention_score']:.2f}</td>"
        f"<td>{_verdict(r['needs_attention'], r['merged'])}</td></tr>"
        for r in teams[:12])

    # Drill-down: offending PRs of the worst repo, with failing checks as links.
    worst = next((s for s in sorted(summaries, key=lambda x: -x['attention_score'])
                  if s['flagged_prs']), None)
    flagged_html = ""
    if worst:
        all_flagged = worst["flagged_prs"]
        shown = all_flagged[:100]
        items = ""
        for pr in shown:
            vio = ", ".join(v.replace("_", " ") for v in pr["violations"])
            checks = ""
            if pr.get("failing_checks"):
                links = ", ".join(
                    (f'<a href="{c["link"]}" target="_blank">{c["name"]}</a>'
                     if c.get("link") else c["name"])
                    for c in pr["failing_checks"])
                checks = f'<div class="checks">failed checks: {links}</div>'
            items += (f"<li><b>#{pr['number']}</b> {(pr['title'] or '')[:60]}"
                      f"<div class='vio'>{vio}</div>{checks}</li>")
        count = (f"all {len(all_flagged)}" if len(shown) == len(all_flagged)
                 else f"{len(shown)} of {len(all_flagged)}")
        flagged_html = (f"<b>Offending PRs in {worst['repo']} (showing {count}):</b>"
                        f"<ul class='flagged'>{items}</ul>")

    if corr["reverted"]:
        corr_rows = ""
        for r in corr["behaviours"]:
            w = f"{r['with_rate']:.1f}%" if r['with_rate'] is not None else "n/a"
            o = f"{r['wo_rate']:.1f}%" if r['wo_rate'] is not None else "n/a"
            corr_rows += f"<tr><td>{r['behaviour'].replace('_',' ')}</td><td>{w}</td><td>{o}</td></tr>"
        corr_html = (f"<p>Outcome = reverted PRs ({corr['reverted']} of {corr['merged']} merged).</p>"
                     f"<table><tr><th>Behaviour</th><th>revert% WITH</th>"
                     f"<th>revert% WITHOUT</th></tr>{corr_rows}</table>")
    else:
        corr_html = "<p>No reverts in this window — fetch more history to measure outcomes.</p>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>PR Health Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<style>
 body{{font-family:system-ui,Arial,sans-serif;margin:0;background:#0e1116;color:#e6e6e6}}
 header{{background:#161b22;padding:24px 32px;border-bottom:1px solid #30363d}}
 h1{{margin:0;font-size:22px}} h2{{font-size:16px;color:#9ecbff;margin-top:0}}
 .sub{{color:#8b949e;font-size:13px}} main{{padding:24px 32px;max-width:980px}}
 section{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px 22px;margin-bottom:20px}}
 table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d;font-size:14px}}
 th{{color:#8b949e;font-weight:600}}
 .badge{{color:#fff;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}}
 .bar{{position:relative;background:#21262d;border-radius:6px;height:18px;width:120px}}
 .bar .fill{{background:#e5484d;height:100%;border-radius:6px}}
 .bar span{{position:absolute;left:8px;top:0;font-size:11px;line-height:18px}}
 ul{{margin:6px 0}} li{{font-size:14px;margin:3px 0}}
 ul.flagged li{{margin:8px 0;list-style:none;border-left:3px solid #e5484d;padding-left:10px}}
 .vio{{color:#f5a524;font-size:12px}} .checks{{font-size:12px;color:#8b949e}}
 .checks a{{color:#9ecbff}} a{{color:#9ecbff}}
</style></head><body>
<header><h1>📊 PR Health Dashboard</h1>
<div class="sub">Generated {generated_at} · powered by Harness SCM data</div></header>
<main>
 {events_html}
 <section><h2>1 · How healthy are our PR practices?</h2>
  <table><tr><th>Repo</th><th>Merged</th><th>No-build</th><th>Violation</th><th>Health</th></tr>
  {rows}</table></section>
 <section><h2>2 · Which repos / teams need attention now?</h2>
  <b>Repositories:</b><ul>{attn}</ul>
  <b>People / teams:</b>
  <table><tr><th>Author / team</th><th>Merged</th><th>Score</th><th>Verdict</th></tr>{team_rows}</table>
  {flagged_html}
 </section>
 <section><h2>3 · What behaviours correlate with quality?</h2>{corr_html}</section>
</main></body></html>"""


def compute(args):
    """Load data from the chosen source and run all three analyses."""
    repos = load_live() if args.live else load_sample(args.data)
    summaries = analyze(repos)
    team_map = {}
    if os.path.exists("teams.json"):
        with open("teams.json") as f:
            team_map = json.load(f)
    return summaries, rollup_by_team(repos, team_map), correlate(repos)


MAIN_BRANCHES = {"main", "master"}


def parse_pr_event(payload):
    """From a Harness Code (or GitHub) webhook payload, extract (action, number, target).
    action is 'opened' | 'merged' | None (event we don't care about)."""
    trigger = str(payload.get("trigger") or payload.get("action") or "").lower()
    pr = (payload.get("pull_req") or payload.get("pull_request")
          or payload.get("pullreq") or {})
    number = pr.get("number") or payload.get("number")
    target = pr.get("target_branch") or (pr.get("base") or {}).get("ref")
    merged_flag = pr.get("merged") or payload.get("merged")
    # Branch events first — "branch_created" also contains "creat", so check it before PR open.
    if "branch" in trigger:                      # a push/merge landing on a branch
        ref = payload.get("ref")
        if isinstance(ref, dict):
            ref = ref.get("name") or ref.get("ref")
        branch = str(ref or payload.get("ref_name") or target or "").split("/")[-1] or None
        return "updated", number, branch          # number is usually None for branch pushes
    if "merg" in trigger or (trigger == "closed" and merged_flag):
        return "merged", number, target
    if "creat" in trigger or "open" in trigger or "reopen" in trigger:
        return "opened", number, target
    return None, number, target


def serve(args, port):
    """Web server: renders the dashboard (GET) and receives PR webhooks (POST /webhook)."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import time as _time

    try:
        summaries, teams, corr = compute(args)
    except Exception as e:                       # don't let a poll failure stop the receiver
        print(f"[warn] initial analytics load failed: {e}")
        summaries, teams, corr = [], [], {"behaviours": [], "merged": 0, "reverted": 0}
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    events = []                                   # most-recent-first live webhook events

    from notify import notify_build_failed, slack_enabled
    notified = set()                              # PR numbers already alerted (dedupe)
    dash_url = os.environ.get("DASHBOARD_URL", f"http://localhost:{port}").rstrip("/")
    if slack_enabled():
        print("[notify] Slack alerts enabled for failed builds")

    def maybe_notify(rec):
        """Send one Slack alert the first time a PR's build is seen as failed."""
        n = rec.get("number")
        if rec.get("build_state") != "failed" or n is None or n in notified:
            return
        notified.add(n)
        notify_build_failed(
            number=n, title=rec.get("title"), target=rec.get("target"),
            author=rec.get("author"), failing_checks=rec.get("failing_checks"),
            pr_url=pr_web_url(n), suggest_url=f"{dash_url}/suggest?pr={n}")

    def handle_event(payload):
        action, number, target = parse_pr_event(payload)
        if not action:
            return f"ignored (event {payload.get('trigger') or payload.get('action')})"
        # Only care about main/master.
        if target and target.lower() not in MAIN_BRANCHES:
            return f"ignored (target {target})"
        # Seed from the webhook payload, then enrich (only PR events have a number).
        pr_obj = (payload.get("pull_req") or payload.get("pull_request")
                  or payload.get("pullreq") or {})
        who = (pr_obj.get("author") or pr_obj.get("user")
               or payload.get("principal") or {})
        title = pr_obj.get("title") or (f"{target} branch updated" if number is None else None)
        rec = {"action": action, "number": number, "target": target,
               "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
               "title": title,
               "author": who.get("email") or who.get("login") or who.get("display_name"),
               "build_state": "unknown", "failing_checks": []}
        if number is not None:
            try:                                  # enrich PR events with full details + checks
                pr = enrich_pr(number)
                rec.update(title=pr.get("title") or rec["title"],
                           author=pr.get("author_email") or rec["author"],
                           build_state=build_state(pr), failing_checks=failing_checks(pr))
            except Exception as e:
                print(f"[warn] could not enrich PR #{number}: {e}")
        maybe_notify(rec)
        events.insert(0, rec)
        del events[50:]
        label = f"PR #{number}" if number is not None else f"branch {target}"
        print(f"[event] {action} {label}")
        return f"recorded: {action} {label}"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
            if self.path.startswith("/suggest"):
                self._suggest(); return
            # Re-fetch current build status for PR events — checks often complete AFTER
            # the PR-opened webhook fired, so the original snapshot can be stale.
            fresh = {}
            for e in events:
                n = e.get("number")
                if n is None:
                    continue
                try:
                    if n not in fresh:
                        pr = enrich_pr(n)
                        fresh[n] = (build_state(pr), failing_checks(pr))
                    e["build_state"], e["failing_checks"] = fresh[n]
                    maybe_notify(e)               # build may have failed after the webhook
                except Exception:
                    pass
            body = build_html(summaries, teams, corr, stamp, events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(body)

        def _suggest(self):
            import urllib.parse, html as _html
            from ai_suggest import suggest_fix
            pr = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("pr", [None])[0]
            title, checks, suggestion = None, None, ""
            try:
                data = enrich_pr(int(pr)) if pr else None
                if data:
                    title = data.get("title")
                    checks = failing_checks(data)
                suggestion = suggest_fix(checks or [], pr_title=title)
            except Exception as e:
                suggestion = f"Could not produce a suggestion: {e}"
            body = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>Fix suggestion — PR #{_html.escape(str(pr))}</title>"
                "<style>body{font-family:system-ui,Arial,sans-serif;margin:0;background:#0e1116;"
                "color:#e6e6e6}main{max-width:820px;margin:0 auto;padding:24px 32px}"
                "a{color:#9ecbff}h1{font-size:20px}.sub{color:#8b949e}"
                "pre{white-space:pre-wrap;background:#161b22;border:1px solid #30363d;"
                "border-radius:10px;padding:18px 22px;font-size:14px;line-height:1.55}</style></head>"
                "<body><main><p><a href='/'>← back to dashboard</a></p>"
                f"<h1>💡 Fix suggestion — PR #{_html.escape(str(pr))}</h1>"
                f"<p class='sub'>{_html.escape(title or '')}</p>"
                f"<pre>{_html.escape(suggestion)}</pre></main></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(body)

        def _build_failed(self):
            # Called by the CI pipeline on failure:  POST /build-failed?pr=<+codebase.prNumber>
            # The pipeline is authoritative that the build failed, so we alert regardless
            # of whether the commit check has been posted yet.
            import urllib.parse
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)           # drain body, if any
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pr = q.get("pr", [None])[0]
            msg = "missing ?pr="
            try:
                if pr is not None:
                    n = int(pr)
                    try:
                        data = enrich_pr(n)
                        rec = {"action": "build", "number": n,
                               "target": data.get("target_branch"),
                               "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                               "title": data.get("title"),
                               "author": data.get("author_email"),
                               "build_state": "failed",   # pipeline said so
                               "failing_checks": failing_checks(data)}
                    except Exception:                 # enrichment failed — still alert
                        rec = {"action": "build", "number": n, "target": None,
                               "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                               "title": None, "author": None,
                               "build_state": "failed", "failing_checks": []}
                    events.insert(0, rec); del events[50:]
                    maybe_notify(rec)
                    msg = f"notified PR #{n}"
                    print(f"[build-failed] {msg}")
            except Exception as e:
                msg = f"error: {e}"
                print(f"[build-failed] {msg}")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(msg.encode())

        def do_POST(self):
            if self.path.startswith("/build-failed"):
                self._build_failed(); return
            if self.path.rstrip("/") != "/webhook":
                self.send_response(404); self.end_headers(); return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            print(f"[webhook] {length} bytes from {self.client_address[0]}: {raw[:600]!r}")
            msg = "ok"
            try:
                payload = json.loads(raw) if raw.strip() else {}
                if not isinstance(payload, dict):
                    payload = {}
                msg = handle_event(payload)
            except Exception as e:
                import traceback
                traceback.print_exc()
                msg = f"logged error: {e}"
            # Always ack 200 so Harness keeps delivering; problems are in the server log.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass

    print(f"PR Health Dashboard on http://0.0.0.0:{port}  "
          f"(GET / = dashboard, POST /webhook = events, "
          f"POST /build-failed?pr=N = CI failure alert, GET /healthz = probe)")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="PR Health Analyzer")
    ap.add_argument("--data", default="sample_data.json", help="path to sample data JSON")
    ap.add_argument("--live", action="store_true", help="read live data from Harness")
    ap.add_argument("--json", action="store_true", help="output JSON instead of a table")
    ap.add_argument("--html", metavar="FILE", help="write the HTML dashboard to FILE")
    ap.add_argument("--serve", action="store_true", help="run as a web server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args()

    load_dotenv()  # pull HARNESS_* config from a local .env file if present

    if args.serve:
        serve(args, args.port)
        return

    summaries, teams, corr = compute(args)

    if args.html:
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        with open(args.html, "w") as f:
            f.write(build_html(summaries, teams, corr, stamp))
        print(f"Wrote dashboard to {args.html}")
    elif args.json:
        print(json.dumps({"repositories": summaries, "teams": teams,
                          "correlation": corr}, indent=2))
    else:
        render_table(summaries)
        render_attention(summaries, teams)
        render_correlation(corr)


if __name__ == "__main__":
    main()
