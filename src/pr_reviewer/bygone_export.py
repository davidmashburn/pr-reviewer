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
_ANCHOR_LINES = 3  # lines of literal text an anchor matches on


def _anchor_text(files: list[FileDiff], anchor: Anchor) -> str:
    """Literal new-file text at the anchor's line range, for a `contains` match.
    "" when the range isn't present in the stored diff rows (anchor dropped).

    Picks the longest line in range: `contains` must match one place in the
    file, and short lines (a closing brace, a repeated table field) collide."""
    numbered: list[tuple[int, str]] = []
    for f in files:
        if f.path != anchor.file:
            continue
        for row in f.rows:
            if row.n and anchor.start <= row.n[0] <= anchor.end:
                numbered.append((row.n[0], row.n[1]))
    numbered.sort()
    # a single line repeats too easily (a closing brace, a repeated table field);
    # a run of adjacent lines is the same literal text and far more likely unique
    block: list[str] = []
    for i, (line_no, text) in enumerate(numbered):
        if not block:
            if not text.strip():
                continue
            block = [text]
            prev = line_no
            continue
        if line_no != prev + 1 or len(block) >= _ANCHOR_LINES:
            break
        block.append(text)
        prev = line_no
    return "\n".join(block)


def _occurrence(files: list[FileDiff], anchor: Anchor, text: str) -> tuple[int, int]:
    """(index, total) for `text` in the file, counted over the changed rows.
    Correct only when every repeat is itself part of the diff."""
    index, total = 0, 0
    for f in files:
        if f.path != anchor.file:
            continue
        for row in sorted((r for r in f.rows if r.n), key=lambda r: r.n[0]):
            if row.n[1] == text:
                total += 1
                if not index and row.n[0] >= anchor.start:
                    index = total
    return max(index, 1), total


def build_tour(review: Review) -> dict[str, Any]:
    """The tour document as a plain dict — dump with yaml.safe_dump to serialize."""
    anchors: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []

    def add_anchor(key: str, a: Anchor) -> str | None:
        text = _anchor_text(review.files, a)
        if not text:
            return None
        entry: dict[str, Any] = {"file": a.file, "revision": "head", "contains": text}
        index, total = _occurrence(review.files, a, text)
        if total > 1:  # a repeated line is ambiguous even when the first match is meant
            entry["occurrence"] = index
        anchors[key] = entry
        return key

    for req in review.requirements:
        link = next((l for l in review.links if l.requirement_id == req.id), None)
        if link is None or not link.anchors:
            continue
        key = add_anchor(f"req-{req.id}", link.anchors[0])
        if key is None:
            continue
        body = "\n\n".join(p for p in (link.mechanism, link.why) if p) or req.text
        # the tour schema admits only these enum values; anything else is omitted
        requirement: dict[str, Any] = {"id": req.id, "text": req.text}
        if link.status in ("fulfilled", "gap"):
            requirement["status"] = link.status
        if req.source:
            requirement["source"] = req.source
        if link.confidence in ("high", "medium", "low"):
            requirement["confidence"] = link.confidence
        steps.append({
            "id": key,
            "title": f"{req.id}: {req.text[:60]}",
            "body": body,
            "focus": key,
            "requirement": requirement,
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

    fulfilled = sum(1 for l in review.links if l.status == "fulfilled")
    scene = {
        "id": "review",
        "title": review.pr.title,
        "summary": review.net_effect[0] if review.net_effect else review.pr.title,
        "bullets": [b for b in review.net_effect[1:] if b],
        "tags": ["requirements-review"],
        "takeaway": f"{fulfilled}/{len(review.requirements)} stated requirements are met by this diff.",
        "steps": steps,
    }

    doc: dict[str, Any] = {
        "version": 1,
        "title": review.pr.title,
        "windowTitle": f"{review.pr.repo}#{review.pr.number}",
        "sourceUrl": review.pr.url,
        "anchors": anchors,
        # required by the validator even when the tour draws no anchor-to-anchor relationships
        "connections": {},
        "chapters": [{"id": "review", "title": "Requirements review", "scenes": [scene]}],
    }
    # anchors only resolve against the PR's own range; branch refs are all the
    # review retains, so a consumer needs both refs fetched locally
    if review.pr.branch and review.pr.base_branch:
        doc["range"] = {"base": review.pr.base_branch, "head": review.pr.branch}
    return doc


def review_to_bygone_yaml(review: Review) -> str:
    return yaml.safe_dump(build_tour(review), sort_keys=False, allow_unicode=True, width=100)
