#!/usr/bin/env python3
"""Independent LLM PR reviewer + seriousness router (Phase 1).

On a PR event: read the real diff and scale review *rigor* to the change's blast
radius, then set the required `llm-review` status check.

The whole feature is one number — a **seriousness tier** derived from the changed
files (the deterministic surface floor, §4.1 of the design):

  Tier 0 (docs / tests / UI)  -> one confirm review, auto-pass.
  Tier 1 (ordinary code)      -> one independent confirm review; APPROVE = green.
  Tier 2 (sensitive surface)  -> an ADVERSARIAL multi-lens panel (each reviewer
                                 hunts a distinct failure vector, attack-mode),
                                 and the check is HELD for the Director regardless.

The Tier-2 panel is the workflow that reviewed #133: a single *confirm* review said
APPROVE on a new enrollment-token primitive; a 4-lens *attack* panel on the same PR
found a HIGH (forgeable token under misconfig) + 3 MED the confirm review missed.
Confirmation verifies the guards are *present*; attack verifies they *hold*.

Method-first, model-second: this phase escalates the review *method* (confirm ->
panel). Model-tiering (best-in-vault for Tier 2) is a later phase. The LLM key stays
in the engine vault; this script calls `/api/v1/llm/review`, which runs the model.

Design: docs/design/PR-REVIEW-SERIOUSNESS-ROUTER.md (CES).
Pure functions (seriousness_tier / sensitive_paths / status_for / panel_max_severity
/ build_confirm_comment / build_panel_comment) are unit-tested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Any

STATUS_CONTEXT = "llm-review"

# --- Tier 2: the un-bypassable high-risk surface floor (§4.1). Generous by design:
# a false escalation costs tokens; a false de-escalation ships the takeover. ---
TIER2_PREFIXES: tuple[str, ...] = (
    "CLAUDE.md", "docs/design/", "docs/coordination/", "knowledge/strategy/",
    ".github/", "migrations/", "alembic/",
)
TIER2_SUBSTRINGS: tuple[str, ...] = (
    # auth / identity / tokens
    "auth", "enroll", "session", "webauthn", "passkey", "token", "credential", "login",
    # crypto / secrets
    "crypto", "encrypt", "decrypt", "signing", "signature", "vault", "secret",
    # data shape / tenancy
    "migration", "schema", "tenant", "tenanc", "/rls",
    # provenance / audit
    "ledger", "provenance", "receipt", "attestation", "audit",
    # ci / the router itself / rulings
    "branch-protection", "llm_pr_review", "seriousness", "rulings",
)

# --- Tier 0: clearly low-risk surfaces (only when NO file forces Tier 2). ---
LOW_SUFFIXES: tuple[str, ...] = (
    ".md", ".mdx", ".txt", ".rst", ".css", ".scss", ".svg", ".png", ".jpg", ".jpeg",
    ".ico", ".gif", ".webp", ".tsx", ".jsx", ".vue", ".svelte", ".html",
)
LOW_SUBSTRINGS: tuple[str, ...] = ("/tests/", "/__tests__/", "test_", "_test.", ".test.", ".spec.")

_TIER2_PREFIXES_LOWER: tuple[str, ...] = tuple(p.lower() for p in TIER2_PREFIXES)

SEVERITY_ORDER: tuple[str, ...] = ("NONE", "LOW", "MED", "HIGH")

CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "reasoning"],
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "BLOCK"]},
        "reasoning": {"type": "string"},
    },
}

PANEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["highest_severity", "findings"],
    "properties": {
        "highest_severity": {"type": "string", "enum": ["NONE", "LOW", "MED", "HIGH"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "summary"],
                "properties": {
                    "severity": {"type": "string", "enum": ["LOW", "MED", "HIGH"]},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}

CONFIRM_SYSTEM_PROMPT = (
    "You are an independent code reviewer (NOT the author). Read the diff and judge it: "
    "correctness (defects, broken contracts, off-by-ones), security/secrets/auth, whether "
    "tests cover behaviour changes, and whether the change stays in scope. Return APPROVE only "
    "if you'd stake your name on it shipping; BLOCK on a real defect. Keep reasoning to <=3 "
    "sentences and cite files/lines. Respond with ONLY a JSON object: "
    '{"verdict":"APPROVE"|"BLOCK","reasoning":"..."}.'
)

# The adversarial panel — each lens a distinct account-takeover / integrity vector.
# These map one-to-one onto the four #133 findings.
PANEL_LENSES: tuple[tuple[str, str], ...] = (
    ("forgery",
     "Can a token / key / credential / signed value be FORGED, spoofed, or bypassed? "
     "Hunt dev/test fallbacks or default secrets visible in an open repo, weak or missing "
     "signature verification, predictable values, or a guard that checks the wrong thing."),
    ("authorization",
     "Can a caller cross an AUTH or TENANT boundary? Hunt privilege escalation, wrong-tenant "
     "access, an identity/lookup keyed on a NON-UNIQUE field (e.g. email) that could attach the "
     "wrong user, or a missing ownership/permission check."),
    ("replay_race",
     "Is the change REPLAYABLE or RACE-ABLE? Hunt TOCTOU on a uniqueness or first-time check, "
     "reusable/replayable tokens, over-generous TTLs on live links, a missing DB uniqueness "
     "constraint, or an idempotency gap."),
    ("input_guard",
     "Does it accept UNTRUSTED INPUT on a new path, or REMOVE/WEAKEN a guard or test? Hunt "
     "SSRF/injection, unvalidated parameters, a deleted assertion, or a loosened check."),
)


def _panel_system_prompt(focus: str) -> str:
    return (
        "You are an adversarial security reviewer on a panel, assigned ONE failure vector. "
        "Do NOT merely confirm the code's guards are present — ASSUME they are insufficient and "
        "try to BREAK the change along your vector. "
        f"YOUR VECTOR: {focus} "
        "Report only findings you can substantiate from the diff, each with a severity: "
        "HIGH = exploitable (account takeover / data loss / forgery); MED = a real weakness under "
        "a plausible condition; LOW = hardening. If, after genuinely attacking, you find nothing on "
        "your vector, return an empty findings list and highest_severity NONE. Cite files/lines. "
        'Respond with ONLY: {"highest_severity":"NONE|LOW|MED|HIGH","findings":'
        '[{"severity":"LOW|MED|HIGH","summary":"..."}]}.'
    )


# ---------- pure logic (unit-tested) ----------

def sensitive_paths(files: list[str]) -> list[str]:
    """Files that force Tier 2 (the un-bypassable surface floor). Named in the comment."""
    hits = []
    for f in files:
        low = f.lower()  # case-insensitive so a renamed .GitHub/ or CLAUDE.MD can't slip the floor
        if low.startswith(_TIER2_PREFIXES_LOWER) or any(s in low for s in TIER2_SUBSTRINGS):
            hits.append(f)
    return hits


def _is_low_risk(f: str) -> bool:
    low = f.lower()
    return low.endswith(LOW_SUFFIXES) or any(s in low for s in LOW_SUBSTRINGS)


def seriousness_tier(files: list[str]) -> tuple[int, list[str]]:
    """(tier, tier2_hits). Deterministic surface floor — no model, no judgment.

    Tier 2 if any file is a high-risk surface (un-bypassable). Else Tier 0 if EVERY
    file is clearly low-risk (docs/tests/UI). Else Tier 1 (ordinary code).
    """
    hits = sensitive_paths(files)
    if hits:
        return 2, hits
    if files and all(_is_low_risk(f) for f in files):
        return 0, []
    return 1, []


def panel_max_severity(results: list[dict]) -> str:
    """Highest severity across all panel lenses (NONE if the panel came back clean)."""
    worst = 0
    for res in results:
        sev = str(res.get("highest_severity", "NONE")).upper()
        if sev in SEVERITY_ORDER:
            worst = max(worst, SEVERITY_ORDER.index(sev))
    return SEVERITY_ORDER[worst]


def status_for(tier: int, verdict: str | None = None, panel_severity: str | None = None) -> tuple[str, str]:
    """(github status state, short description) for the llm-review check."""
    if tier == 2:
        sev = (panel_severity or "NONE").upper()
        return "failure", f"Tier 2 (sensitive) — adversarial panel ran (max {sev}); held for Director"
    if tier == 0:
        return "success", "Tier 0 (docs/tests/UI) — independent review, auto-pass"
    if verdict == "APPROVE":
        return "success", "Tier 1 — independent review: APPROVE"
    return "failure", "Tier 1 — independent review: BLOCK, fix before merge"


def build_confirm_comment(tier: int, verdict: str, reasoning: str) -> str:
    green = (verdict == "APPROVE")
    icon = "✅" if green else "⛔"
    label = "Tier 0 · docs/tests/UI" if tier == 0 else "Tier 1 · ordinary code"
    lines = [f"### {icon} Independent review — {label}", "", f"**Verdict:** {verdict}", "", reasoning]
    if tier == 0 and not green:
        lines += ["", "> Tier 0 auto-passes (low blast radius); the note above is advisory only."]
    lines += ["", "<sub>Seriousness router · single confirm review · "
                  "`llm-review` reflects this. See docs/design/PR-REVIEW-SERIOUSNESS-ROUTER.md.</sub>"]
    return "\n".join(lines)


def build_panel_comment(hits: list[str], results: list[tuple[str, dict]]) -> str:
    max_sev = panel_max_severity([r for _, r in results])
    lines = [
        "### 🛡️ Tier 2 — adversarial review panel",
        "",
        f"**Why escalated:** touches a high-risk surface ({', '.join(hits[:8])}"
        f"{' …' if len(hits) > 8 else ''}).",
        f"**Panel max severity:** {max_sev}. **Held for the Director** regardless of severity "
        "(a serious surface always gets human eyes).",
        "",
    ]
    for name, res in results:
        sev = str(res.get("highest_severity", "NONE")).upper()
        findings = res.get("findings") or []
        head = f"**{name}** — {sev}"
        if not findings:
            lines.append(f"- {head}: no finding on this vector.")
        else:
            lines.append(f"- {head}:")
            for fnd in findings[:6]:
                fsev = str(fnd.get("severity", "?")).upper()
                lines.append(f"    - `{fsev}` {fnd.get('summary', '').strip()}")
    lines += ["", "<sub>Seriousness router · 4-lens adversarial panel (attack-mode) · "
                  "`llm-review` held red for the Director. docs/design/PR-REVIEW-SERIOUSNESS-ROUTER.md.</sub>"]
    return "\n".join(lines)


# ---------- I/O (not unit-tested) ----------

def _gh_json(args: list[str]) -> Any:
    return json.loads(subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout)


def _engine_review(engine_url: str, review_token: str, tenant: str, diff: str, context: str,
                   system_prompt: str, schema: dict) -> dict:
    body = json.dumps({
        "tenant": tenant,
        "system_prompt": system_prompt,
        "user_prompt": f"{context}\n\n=== DIFF ===\n{diff[:120000]}",
        "response_schema": schema,
    }).encode()
    req = urllib.request.Request(
        engine_url.rstrip("/") + "/api/v1/llm/review", data=body, method="POST",
        # X-Review-Token is a review-ONLY credential; it never carries merge/admin power.
        headers={"X-Review-Token": review_token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=150) as resp:
        return json.load(resp)


def run_panel(engine_url: str, token: str, tenant: str, diff: str, context: str) -> list[tuple[str, dict]]:
    """Run each attack lens as an independent review. Sequential — Tier 2 is the minority."""
    out: list[tuple[str, dict]] = []
    for name, focus in PANEL_LENSES:
        try:
            res = _engine_review(engine_url, token, tenant, diff, context,
                                 _panel_system_prompt(focus), PANEL_SCHEMA)
        except Exception as exc:  # a lens failing must not silently green the PR
            res = {"highest_severity": "MED",
                   "findings": [{"severity": "MED", "summary": f"lens '{name}' errored ({exc}); "
                                 "treat as unreviewed — human must check this vector."}]}
        out.append((name, res))
    return out


def main() -> int:
    repo = os.environ["REPO"]
    pr = os.environ["PR_NUMBER"]
    engine_url = os.environ.get("ENGINE_URL", "").strip()
    engine_key = os.environ.get("ENGINE_KEY", "").strip()   # the review-only token
    tenant = os.environ.get("REVIEW_TENANT", "").strip()
    if not engine_url or not engine_key or not tenant:
        print("llm-pr-review: ENGINE_URL / ENGINE_KEY / REVIEW_TENANT not configured — skipping.")
        return 0

    info = _gh_json(["pr", "view", pr, "--repo", repo, "--json", "title,files,headRefOid,body"])
    files = [f["path"] for f in info.get("files", [])]
    head_sha = info["headRefOid"]
    diff = subprocess.run(["gh", "pr", "diff", pr, "--repo", repo],
                          capture_output=True, text=True, check=True).stdout

    tier, hits = seriousness_tier(files)
    context = (f"PR {repo}#{pr}: {info.get('title')}\nFiles: {', '.join(files[:50])}\n\n"
               f"{(info.get('body') or '')[:3000]}")

    if tier == 2:
        results = run_panel(engine_url, engine_key, tenant, diff, context)
        comment = build_panel_comment(hits, results)
        state, desc = status_for(2, panel_severity=panel_max_severity([r for _, r in results]))
    else:
        result = _engine_review(engine_url, engine_key, tenant, diff, context,
                                CONFIRM_SYSTEM_PROMPT, CONFIRM_SCHEMA)
        verdict = str(result.get("verdict", "BLOCK")).upper()
        reasoning = str(result.get("reasoning", "(no reasoning returned)"))
        comment = build_confirm_comment(tier, verdict, reasoning)
        state, desc = status_for(tier, verdict=verdict)

    subprocess.run(["gh", "pr", "comment", pr, "--repo", repo, "--body", comment], check=True)
    subprocess.run(["gh", "api", "-X", "POST", f"/repos/{repo}/statuses/{head_sha}",
                    "-f", f"state={state}", "-f", f"context={STATUS_CONTEXT}",
                    "-f", f"description={desc[:140]}"], check=True)
    print(f"llm-review: tier={tier} {state} — {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
