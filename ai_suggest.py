#!/usr/bin/env python3
"""
AI fix suggester — asks Claude how to make a failing CI build pass.

Given a PR's failing check names (+ their summaries / log text), Claude returns
concrete, actionable fix steps. Single Messages API call, no agent loop.

Needs:  pip install anthropic   and   ANTHROPIC_API_KEY in the environment / .env
"""
import os

MODEL = "claude-opus-4-8"  # first-party model id; override with ANTHROPIC_MODEL
# On Amazon Bedrock the model id needs the provider prefix; override with BEDROCK_MODEL.
BEDROCK_MODEL = "anthropic.claude-opus-4-8"

SYSTEM = (
    "You are a senior CI/build engineer helping a developer get a failing pull "
    "request build to pass. You are given the PR title, the names of the checks "
    "that failed, their summaries, and any available log output. "
    "Respond with: (1) one line naming the most likely cause, then (2) numbered, "
    "concrete fix steps with code/config snippets where useful. Be concise and "
    "specific. If the cause is ambiguous, list the most likely causes in priority "
    "order. Do NOT invent log lines or errors that weren't provided."
)


def _make_client():
    """Pick the right client based on the key type.
    - 'ABSK...' / AWS_BEARER_TOKEN_BEDROCK  -> Amazon Bedrock (needs anthropic[bedrock])
    - otherwise                             -> first-party Anthropic (sk-ant-...)
    Returns (client, model) or (None, error_string).
    """
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    is_bedrock = key.startswith("ABSK") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if is_bedrock:
        try:
            from anthropic import AnthropicBedrock
        except ImportError:
            return None, "Bedrock mode needs:  pip install 'anthropic[bedrock]'"
        # Bedrock bearer-token auth is read from this env var by the AWS layer.
        if key:
            os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", key)
        region = os.environ.get("BEDROCK_AWS_REGION", "us-east-1")
        return AnthropicBedrock(aws_region=region), os.environ.get("BEDROCK_MODEL", BEDROCK_MODEL)
    return anthropic.Anthropic(), os.environ.get("ANTHROPIC_MODEL", MODEL)


def suggest_fix(failing_checks, pr_title=None, log_text=None):
    """failing_checks: list of {name, summary, link}. Returns a suggestion string."""
    try:
        import anthropic
    except ImportError:
        return "AI suggestions need the 'anthropic' package:  pip install anthropic"
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return ("Set ANTHROPIC_API_KEY in .env to enable AI fix suggestions "
                "(first-party key from console.anthropic.com, or an ABSK… Bedrock key).")

    lines = []
    if pr_title:
        lines.append(f"PR title: {pr_title}")
    lines.append("Failing checks:")
    for c in (failing_checks or []):
        lines.append(f"  - {c.get('name')}: {c.get('summary') or '(no summary provided)'}")
    if log_text:
        lines.append("\nStep log (tail):\n" + log_text[-4000:])
    lines.append("\nSuggest concrete changes to make this build pass.")
    prompt = "\n".join(lines)

    client, model = _make_client()
    if client is None:
        return model  # error string
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            thinking={"type": "adaptive"},   # let Claude decide how much to reason
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        return f"AI request failed ({e.status_code}): {getattr(e, 'message', e)}"
    except Exception as e:
        return f"AI request failed: {e}"

    return "".join(b.text for b in resp.content if b.type == "text").strip() \
        or "(no suggestion returned)"
