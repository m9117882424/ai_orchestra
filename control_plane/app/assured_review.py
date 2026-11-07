"""Durable, fail-closed identity for isolated high-assurance read-only reviews."""

from __future__ import annotations

import json
import re


ASSURED_REVIEW_BACKEND = "opencode-review"
ASSURED_REVIEW_AGENT = "assured-reviewer"
ASSURED_REVIEW_ROLES = [ASSURED_REVIEW_AGENT]
_VERDICT = re.compile(
    r"<AI_ORCHESTRA_VERDICT>\s*(\{[^<>]{1,4096}\})\s*</AI_ORCHESTRA_VERDICT>",
    re.DOTALL,
)


def parse_assured_review_verdict(result: str) -> dict | None:
    """Accept one bounded machine-verifiable verdict without storing raw text."""
    matches = _VERDICT.findall(result)
    if len(matches) != 1:
        return None
    try:
        data = json.loads(matches[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or set(data) != {"version", "verdict", "findings_count"}:
        return None
    if data["version"] != 1 or type(data["findings_count"]) is not int:
        return None
    if not 0 <= data["findings_count"] <= 1000:
        return None
    if data["verdict"] not in {"pass", "fail", "blocked"}:
        return None
    if data["verdict"] == "pass" and data["findings_count"]:
        return None
    return data
