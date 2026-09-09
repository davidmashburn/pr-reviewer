"""Export a Review as a Bygone change-tour YAML document.

Format: https://github.com/hsiaotienfan/bygone/blob/main/docs/change-tour-format.md
Mapping: net_effect -> scene summary/bullets; each requirement link and each
bug finding -> one step, anchored at its first evidence anchor. `contains` is
recovered from the stored diff rows (literal new-file text), not invented.
"""
from __future__ import annotations

from typing import Any

import yaml

from .models import Anchor, FileDiff, Review

_SEV_LABEL = {"blocker": "BLOCKER", "major": "MAJOR", "minor": "MINOR", "nit": "NIT"}


def _anchor_text(files: list[FileDiff], anchor: Anchor) -> str:
    """Literal new-file text at the anchor's line range, for a `contains` match.
    "" when the range isn't present in the stored diff rows (anchor dropped)."""
    for f in files:
        if f.path != anchor.file:
            continue
        for row in f.rows:
            if row.n and anchor.start <= row.n[0] <= anchor.end and row.n[1].strip():
                return row.n[1]
    return ""


def build_tour(review: Review) -> dict[str, Any]:
    """The tour document as a plain dict — dump with yaml.safe_dump to serialize."""
    anchors: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []

    def add_anchor(key: str, a: Anchor) -> str | None:
        text = _anchor_text(review.files, a)
        if not text:
            return None
        anchors[key] = {"file": a.file, "revision": "head", "contains": text}
        return key

    for req in review.requirements:
        link = next((l for l in review.links if l.requirement_id == req.id), None)
        if link is None or not link.anchors:
            continue
        key = add_anchor(f"req-{req.id}", link.anchors[0])
        if key is None:
            continue
        body = "\n\n".join(p for p in (link.mechanism, link.why) if p) or req.text
        steps.append({
            "id": key,
            "title": f"{req.id}: {req.text[:60]}",
            "body": body,
            "focus": key,
            "requirement": {
                "id": req.id, "text": req.text, "status": link.status,
                "source": req.source, "confidence": link.confidence,
            },
        })

    for bug in review.bugs:
        if not bug.anchors:
            continue
        key = add_anchor(f"bug-{bug.id}", bug.anchors[0])
        if key is None:
            continue
        sev = _SEV_LABEL.get(bug.severity, bug.severity.upper())
        steps.append({
            "id": key,
            "title": f"[{sev}] {bug.title}",
            "body": bug.detail or bug.title,
            "focus": key,
        })

    scene = {
        "id": "review",
        "title": review.pr.title,
        "summary": review.net_effect[0] if review.net_effect else review.pr.title,
        "bullets": review.net_effect[1:],
        "steps": steps,
    }

    return {
        "version": 1,
        "title": review.pr.title,
        "windowTitle": f"{review.pr.repo}#{review.pr.number}",
        "sourceUrl": review.pr.url,
        "anchors": anchors,
        "chapters": [{"id": "review", "title": "Requirements review", "scenes": [scene]}],
    }


def review_to_bygone_yaml(review: Review) -> str:
    return yaml.safe_dump(build_tour(review), sort_keys=False, allow_unicode=True, width=100)
