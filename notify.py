#!/usr/bin/env python3
"""
Slack notifications for failed PR builds.

Stdlib only (urllib) — no extra dependency. Enabled by setting SLACK_WEBHOOK_URL
in the environment / .env to a Slack Incoming Webhook URL:

  1. Slack -> Apps -> "Incoming Webhooks" -> Add to a channel
  2. Copy the https://hooks.slack.com/services/XXX/YYY/ZZZ URL
  3. Put it in .env:  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

If SLACK_WEBHOOK_URL is unset, every call is a graceful no-op (feature off).
"""
import json
import os
import urllib.request


def slack_enabled():
    return bool(os.environ.get("SLACK_WEBHOOK_URL"))


def _post(payload):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False, "SLACK_WEBHOOK_URL not set"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, r.status
    except Exception as e:
        return False, str(e)


def notify_build_failed(number=None, title=None, target=None, author=None,
                        failing_checks=None, pr_url=None, suggest_url=None):
    """Post a 'build failed' alert to Slack. No-op if SLACK_WEBHOOK_URL is unset."""
    if not slack_enabled():
        return False

    checks = ", ".join(c.get("name", "?") for c in (failing_checks or [])) or "build failed"
    head = f":rotating_light: Build failed — PR #{number}" if number else ":rotating_light: Build failed"
    lines = [f"*{title or '(no title)'}*",
             f"target `{target or '?'}`  ·  {author or 'unknown'}",
             f"failing checks: {checks}"]
    links = []
    if pr_url:
        links.append(f"<{pr_url}|View PR>")
    if suggest_url and number:
        links.append(f"<{suggest_url}|:bulb: AI fix suggestion>")
    if links:
        lines.append("   ".join(links))

    payload = {
        "text": head,                       # fallback / notification preview
        "blocks": [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": head + "\n" + "\n".join(lines)}},
        ],
    }
    ok, info = _post(payload)
    print(f"[notify] slack {'sent' if ok else 'FAILED'}: {info}")
    return ok


if __name__ == "__main__":
    # Quick test:  SLACK_WEBHOOK_URL=... python3 notify.py
    notify_build_failed(number=42, title="Test alert from PR Health Analyzer",
                        target="main", author="you@example.com",
                        failing_checks=[{"name": "ci/unit-tests"}],
                        pr_url="https://app.harness.io")
