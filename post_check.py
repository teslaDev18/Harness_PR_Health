#!/usr/bin/env python3
"""
Post a status check onto a PR's commit via the Harness Code API.

This lets an external system (like this analyzer) report a build status WITHOUT a
CI pipeline — it's the same mechanism external CI tools use to report back. Useful
when an account has no build infrastructure (no Harness Cloud / no delegate).

Usage:
  python post_check.py <pr_number>                 # posts a 'success' check
  python post_check.py <pr_number> failure         # posts a 'failure' check
  python post_check.py 3 failure -m "unit tests failed" --name ci/tests

Reads the same .env as pr_health.py (HARNESS_BASE_URL/ACCOUNT/ORG/PROJECT/REPO/API_KEY).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from pr_health import harness_config, load_dotenv


def main():
    ap = argparse.ArgumentParser(description="Post a Harness Code PR status check")
    ap.add_argument("pr_number", type=int)
    ap.add_argument("status", nargs="?", default="success",
                    choices=["success", "failure", "running", "error", "pending"])
    ap.add_argument("-m", "--summary", default="Reported by PR Health Analyzer")
    ap.add_argument("--name", default="pr-health-analyzer", help="check identifier")
    ap.add_argument("--link", default="https://app.harness.io", help="required by the API")
    args = ap.parse_args()

    load_dotenv()
    base, headers, scope = harness_config()
    repo = os.environ.get("HARNESS_REPO")
    if not repo:
        sys.exit("Set HARNESS_REPO in .env")
    sp = urllib.parse.urlencode(scope)

    def api(path, method="GET", body=None):
        h = dict(headers)
        if body is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{base}/{path}?{sp}", data=body, method=method, headers=h)
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            sys.exit(f"API {e.code} on {method} {path}: {e.read().decode()[:300]}")

    pr = api(f"repos/{repo}/pullreq/{args.pr_number}")
    sha = pr.get("source_sha") or pr.get("merge_base_sha")
    if not sha:
        sys.exit(f"No commit sha found for PR #{args.pr_number}")

    payload = json.dumps({"check_uid": args.name, "status": args.status,
                          "summary": args.summary, "link": args.link}).encode()
    d = api(f"repos/{repo}/checks/commits/{sha}", method="PUT", body=payload)
    print(f"✓ posted check '{d.get('identifier')}' = {d.get('status')} "
          f"on PR #{args.pr_number} (commit {sha[:10]})")
    print("  Re-trigger the PR (or refresh the dashboard) to see the build status update.")


if __name__ == "__main__":
    main()
