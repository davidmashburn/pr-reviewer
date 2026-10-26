"""Versioned, one-way evidence export. Checklist and publication state stay here."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from .models import Review


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
