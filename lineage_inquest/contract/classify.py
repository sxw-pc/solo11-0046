"""Finding drafts and severity assignment."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import DEFAULT_SEVERITY
from ..graph.stable import finding_id
from ..models import Finding


@dataclass(frozen=True)
class FindingDraft:
    category: str
    target: str
    message: str
    detail: str
    script: str = ""
    line: int = 0


def classify(
    draft: FindingDraft,
    severity: dict[str, str],
    overrides: dict[str, str],
) -> Finding:
    default = DEFAULT_SEVERITY[draft.category]
    actual = overrides.get(draft.category, severity.get(draft.category, default))
    return Finding(
        finding_id=finding_id(draft.category, draft.target, draft.detail),
        category=draft.category,
        severity=actual,
        default_severity=default,
        overridden=draft.category in overrides,
        target=draft.target,
        message=draft.message,
        detail=draft.detail,
        script=draft.script,
        line=draft.line,
    )
