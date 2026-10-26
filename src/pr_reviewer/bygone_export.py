"""Export a Review as Bygone evidence.

Two exports live here:

- Change-tour YAML (`build_tour` / `review_to_bygone_yaml`).
  Format: https://github.com/hsiaotienfan/bygone/blob/main/docs/change-tour-format.md
  Mapping: net_effect -> scene summary/bullets; each requirement link with
  evidence and each bug finding -> one step, anchored at its first evidence
  anchor. `contains` is recovered from the stored diff rows (literal new-file
  text), not invented. A requirement with no evidence anchor (partial/notfound)
  has no code to focus a step on, so it's listed in the scene's bullets instead.

- Versioned, one-way evidence export (`build_bygone_v2`). Checklist and
  publication state stay here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import yaml

from .models import Anchor, FileDiff, Review

_SEV_LABEL = {"blocker": "BLOCKER", "major": "MAJOR", "minor": "MINOR", "nit": "NIT"}
_ANCHOR_LINES = 3  # lines of literal text an anchor matches on
# pr-reviewer's Status is fulfilled/partial/notfound; the tour schema's requirement.status
# is fulfilled/gap. Both non-fulfilled values mean "not fully met" from the reviewer's diff.
_BYGONE_STATUS = {"fulfilled": "fulfilled", "partial": "gap", "notfound": "gap"}


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

    unaddressed: list[str] = []
    for req in review.requirements:
        link = next((l for l in review.links if l.requirement_id == req.id), None)
        if link is None:
            continue
        key = add_anchor(f"req-{req.id}", link.anchors[0]) if link.anchors else None
        if key is None:
            # nothing in the diff to anchor a step on (typically notfound/partial);
            # a tour step requires a focus anchor, so record it in prose instead
            reason = f" — {link.missing}" if link.missing else ""
            unaddressed.append(f"{req.id} ({_BYGONE_STATUS[link.status]}, {req.source}): {req.text}{reason}")
            continue
        body = "\n\n".join(p for p in (link.mechanism, link.why) if p) or req.text
        requirement: dict[str, Any] = {
            "id": req.id, "text": req.text, "status": _BYGONE_STATUS[link.status],
        }
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
    bullets = [b for b in review.net_effect[1:] if b]
    if unaddressed:
        bullets.append("Not addressed by this diff:")
        bullets.extend(f"  {line}" for line in unaddressed)
    scene = {
        "id": "review",
        "title": review.pr.title,
        "summary": review.net_effect[0] if review.net_effect else review.pr.title,
        "bullets": bullets,
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


def source_ref(text: str, revision: str, path: str, first: int, last: int) -> dict[str, Any]:
    if re.match(r'^[A-Za-z]:', path) or path.startswith('/') or '\\' in path or '\0' in path or any(p in ('', '.', '..') for p in path.split('/')):
        raise ValueError('Invalid source path')
    if len(text.encode('utf-8')) > 1024 * 1024 or '\0' in text:
        raise ValueError('Source must be text up to 1 MiB')
    # Only LF separates Git/VS Code lines; CR is retained, not normalized.
    offsets = [0] + [i + 1 for i, char in enumerate(text) if char == '\n' and i + 1 < len(text)]
    if first < 1 or last < first or last > len(offsets):
        raise ValueError('Anchor exceeds pinned source')
    start, end = offsets[first - 1], offsets[last] if last < len(offsets) else len(text)
    if start == end:
        raise ValueError('Empty source anchor')
    return {'revision': revision, 'path': path,
            'start': len(text[:start].encode('utf-16-le')) // 2,
            'end': len(text[:end].encode('utf-16-le')) // 2,
            'digest': hashlib.sha256(text[start:end].encode('utf-8')).hexdigest()}


async def build_bygone_v2(review: Review, fetch_file: Callable[[str, str, str], Awaitable[str | None]]) -> dict[str, Any]:
    capture = review.capture
    if capture is None:
        raise ValueError('Review has no immutable capture. Rerun with an evidence-capable provider; current HEAD cannot repair historical provenance.')
    if (capture.provider, capture.repository, capture.number) != (review.pr.provider, review.pr.repo, review.pr.number):
        raise ValueError('Review repository identity differs from immutable capture')
    if len(review.requirements) > 1000 or len(review.links) > 1000 or len(review.unexplained) > 1000:
        raise ValueError('Review exceeds export record budget')
    sources = {s.tag: s for s in review.sources}
    requirements = []
    for requirement in review.requirements:
        source = sources.get(requirement.source)
        if not source or not requirement.quote.strip() or requirement.quote not in source.text:
            raise ValueError(f'{requirement.id}: original source quotation unavailable; repair provenance before export')
        requirements.append({'id': requirement.id, 'text': requirement.text, 'sources': [
            {'title': source.title or source.tag, 'text': source.text, 'quote': requirement.quote}]})
    if len({r['id'] for r in requirements}) != len(requirements):
        raise ValueError('Duplicate requirement IDs')
    known = {r['id'] for r in requirements}
    limitations = list(capture.limitations) + [
        'PR Reviewer model assessments are navigation rationale, not Bygone fulfillment judgments.',
        'Checklist, discussion, approval, and publication state are not exported.',
        'No test execution records were supplied. New-file anchors cannot represent deletion-only evidence.',
    ]
    if review.flow.dropped:
        limitations.append(f'Producer dropped {review.flow.dropped} flow nodes.')
    limitations.append('Inferred flow and architecture are not exported as source or runtime evidence.')
    for change in review.unexplained:
        locations = ', '.join(f'{a.file}:{a.start}-{a.end}' for a in change.anchors)
        hunks = ', '.join(change.hunk_ids)
        limitations.append(f'Producer-reported unexplained change {change.id}: {change.label}. '
                           f'{change.mechanism} Locations (unvalidated producer line labels): {locations or "none"}; '
                           f'hunks: {hunks or "none"}. File-level linked coverage does not cover this change.')
    for section, count in sorted(review.overflow.items()):
        if count:
            limitations.append(f'Producer omitted {count} items from {section}; exported evidence is incomplete.')
    cache: dict[str, str | None] = {}
    invalid: dict[str, str] = {}
    links = []
    total_bytes = 0
    for link in review.links:
        if link.requirement_id not in known:
            raise ValueError('Link refers to an unknown requirement')
        if not link.anchors:
            limitations.append(f'{link.requirement_id}: no new-file source anchors; hunk labels alone are not immutable references.')
        for anchor in link.anchors:
            if len(links) >= 1000:
                raise ValueError('Export exceeds 1000 source links')
            if anchor.file not in cache:
                if len(cache) >= 50:
                    raise ValueError('Export exceeds 50 source files')
                try:
                    # Validate before provider access, including drive and traversal paths.
                    source_ref('x', capture.head_oid, anchor.file, 1, 1)
                    cache[anchor.file] = await fetch_file(capture.repository, capture.head_oid, anchor.file)
                except ValueError as error:
                    cache[anchor.file] = None
                    invalid[anchor.file] = str(error)
                total_bytes += len((cache[anchor.file] or '').encode('utf-8'))
                if total_bytes > 16 * 1024 * 1024:
                    raise ValueError('Export exceeds 16 MiB of source text')
            text = cache[anchor.file]
            try:
                if text is None:
                    raise ValueError(invalid.get(anchor.file, 'source absent at captured head'))
                ref = source_ref(text, capture.head_oid, anchor.file, anchor.start, anchor.end)
            except ValueError as error:
                limitations.append(f'{link.requirement_id}: {anchor.file}: {error}')
                continue
            rationale = ' '.join(filter(None, [link.mechanism, link.why, link.missing])) or 'Inspect the producer-linked source.'
            links.append({'id': f'pr-link-{len(links) + 1}', 'requirement': link.requirement_id,
                          'role': 'implementation', 'rationale': f'{rationale} Producer assessment: {link.status} ({link.confidence}); independently inspect.', 'ref': ref})
    payload = {'version': 2, 'repository': f'{review.pr.provider}:{review.pr.repo}',
            'base': capture.merge_base_oid, 'head': capture.head_oid, 'requirements': requirements,
            'questions': [], 'links': links, 'runs': [], 'decisions': [],
            'analyses': [{'base': capture.merge_base_oid, 'head': capture.head_oid, 'producer': 'pr-reviewer/bygone-v2',
                          'status': 'partial', 'artifactDigest': '0' * 64,
                          'scope': [f'PR {review.pr.url}', f'Provider base tip: {capture.base_oid}',
                                    f'Comparison: {capture.diff_spec}', f'Captured at: {capture.captured_at}',
                                    f'Diff SHA-256: {capture.diff_sha256}', f'Metadata SHA-256: {capture.metadata_sha256}',
                                    'Receipt digest: SHA-256 of the initial exported JSON with this artifactDigest set to 64 zeros; sorted object keys, UTF-8, no ASCII escaping, compact separators. Identifies the producer projection, not authentication or later edits.'],
                          'limitations': limitations}]}

    # Enforce the consumer's string/list bounds in UTF-16 units before sending.
    # Fail explicitly rather than truncating evidence or a quoted input.
    def bounded(value):
        if isinstance(value, str):
            if not value.strip() or len(value.encode('utf-16-le')) // 2 > 100_000:
                raise ValueError('Export contains an empty or oversized evidence string')
        elif isinstance(value, list):
            if len(value) > 1000:
                raise ValueError('Export exceeds 1000 records in an evidence collection')
            for item in value:
                bounded(item)
        elif isinstance(value, dict):
            for item in value.values():
                bounded(item)
    bounded(payload)
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    if len(serialized) > 16 * 1024 * 1024:
        raise ValueError('Export exceeds 16 MiB artifact budget')
    # Hash only the exported evidence projection, never checklist/publication state.
    payload['analyses'][0]['artifactDigest'] = hashlib.sha256(serialized).hexdigest()
    return payload
