import hashlib

import httpx
import pytest

from pr_reviewer.bygone_export import build_bygone_v2, source_ref
from pr_reviewer.models import Anchor, Link, PRCapture, PRInfo, Requirement, Review, SourceText
from pr_reviewer.providers.github import GitHubProvider

BASE, HEAD, MERGE = 'a' * 40, 'b' * 40, 'c' * 40
DIFF = 'diff --git a/a.ts b/a.ts\n'


def review():
    return Review(id='github:example/repo:1', pr=PRInfo(provider='github', repo='example/repo', number=1, url='https://github.com/example/repo/pull/1', title='Fixture'), mode='requirements',
        capture=PRCapture(provider='github', repository='example/repo', number=1, base_oid=BASE, head_oid=HEAD, merge_base_oid=MERGE, diff_sha256=hashlib.sha256(DIFF.encode()).hexdigest(), metadata_sha256='d' * 64, diff_bytes=len(DIFF), captured_at='2026-01-01T00:00:00Z'),
        requirements=[Requirement(id='R1', text='Return two', source='pr-description', quote='Return two')],
        sources=[SourceText(tag='pr-description', title='Synthetic requirement', text='Return two')],
        links=[Link(requirement_id='R1', status='fulfilled', confidence='high', anchors=[Anchor(file='a.ts', start=2, end=2)])], verified=['R1'])


async def test_export_keeps_provenance_and_ignores_checklist():
    captured = []
    text = '// 😀\r\nexport const a = 2;\r\n'

    async def fetch(repo, revision, path):
        captured.append((repo, revision, path))
        return text

    payload = await build_bygone_v2(review(), fetch)
    assert payload['base'] == MERGE and payload['head'] == HEAD
    assert payload['decisions'] == [] and payload['runs'] == []
    assert payload['links'][0]['ref']['start'] == 7
    assert payload['links'][0]['ref']['digest'] == hashlib.sha256('export const a = 2;\r\n'.encode()).hexdigest()
    assert captured == [('example/repo', HEAD, 'a.ts')]
    assert payload['analyses'][0]['status'] == 'partial'


async def test_legacy_and_missing_quotes_fail_closed():
    async def forbidden(*args):
        pytest.fail('must not fetch live data')

    legacy = review().model_copy(update={'capture': None})
    assert Review.model_validate_json(legacy.model_dump_json()).capture is None
    with pytest.raises(ValueError, match='no immutable capture'):
        await build_bygone_v2(legacy, forbidden)
    bad = review()
    bad.requirements[0].quote = 'invented'
    with pytest.raises(ValueError, match='quotation unavailable'):
        await build_bygone_v2(bad, forbidden)


async def test_invalid_anchor_is_visible_as_limitation():
    async def fetch(*args):
        return 'one line\n'
    payload = await build_bygone_v2(review(), fetch)
    assert payload['requirements'] and not payload['links']
    assert 'Anchor exceeds' in '\n'.join(payload['analyses'][0]['limitations'])
    with pytest.raises(ValueError, match='path'):
        source_ref('abc', HEAD, '../x', 1, 1)


def metadata(title='Fixture'):
    return {'number': 1, 'title': title, 'body': 'Return two', 'base': {'sha': BASE, 'ref': 'main'}, 'head': {'sha': HEAD, 'ref': 'feature'}}


async def test_capture_pins_diff_and_guards_metadata_race(monkeypatch):
    calls = []
    count = 0

    def handler(request):
        nonlocal count
        calls.append((request.url.path, request.headers['accept']))
        if '/pulls/' in request.url.path:
            count += 1
            return httpx.Response(200, json=metadata('Fixture' if count == 1 else 'Revised'))
        if request.headers['accept'] == 'application/vnd.github.diff':
            assert request.url.path.endswith(f'{MERGE}...{HEAD}')
            return httpx.Response(200, content=DIFF.encode())
        assert request.url.path.endswith(f'{BASE}...{HEAD}')
        return httpx.Response(200, json={'merge_base_commit': {'sha': MERGE}})

    client = httpx.AsyncClient
    monkeypatch.setattr('pr_reviewer.providers.github.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    result = await GitHubProvider().fetch('example/repo', 1)
    assert count == 4  # first attempt raced, second was coherent
    assert result.title == 'Revised'
    assert result.capture.diff_sha256 == hashlib.sha256(DIFF.encode()).hexdigest()
    assert result.capture.merge_base_oid == MERGE
    assert all('/compare/' in url for url, accept in calls if accept.endswith('.diff'))


async def test_capture_fails_on_repeated_race(monkeypatch):
    count = 0
    def handler(request):
        nonlocal count
        if '/pulls/' in request.url.path:
            count += 1
            return httpx.Response(200, json=metadata(str(count)))
        if request.headers['accept'].endswith('.diff'):
            return httpx.Response(200, text=DIFF)
        return httpx.Response(200, json={'merge_base_commit': {'sha': MERGE}})
    client = httpx.AsyncClient
    monkeypatch.setattr('pr_reviewer.providers.github.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ValueError, match='changed during'):
        await GitHubProvider().fetch('example/repo', 1)


async def test_export_digest_excludes_mutable_state_and_rejects_foreign_identity():
    async def fetch(*args):
        return '// header\nexport const a = 2;\n'
    original = review()
    first = await build_bygone_v2(original, fetch)
    import json
    normalized = json.loads(json.dumps(first))
    normalized['analyses'][0]['artifactDigest'] = '0' * 64
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    assert hashlib.sha256(encoded).hexdigest() == first['analyses'][0]['artifactDigest']
    original.verified = []
    original.published_url = 'https://example.invalid/comment'
    original.threads = {'R1': []}
    assert await build_bygone_v2(original, fetch) == first
    original.pr.repo = 'other/repository'
    with pytest.raises(ValueError, match='identity differs'):
        await build_bygone_v2(original, fetch)


async def test_export_bounds_and_omitted_inputs_are_explicit():
    async def fetch(*args):
        return '// header\nexport const a = 2;\n'
    value = review()
    value.overflow = {'requirements': 3, 'skipped_generated': 2}
    payload = await build_bygone_v2(value, fetch)
    assert 'skipped_generated' in '\n'.join(payload['analyses'][0]['limitations'])
    value.sources[0].text += 'x' * 100000
    with pytest.raises(ValueError, match='oversized'):
        await build_bygone_v2(value, fetch)
    value = review()
    value.links = [Link(requirement_id='R1', status='notfound', confidence='low')] * 1000
    with pytest.raises(ValueError, match='1000 records'):
        await build_bygone_v2(value, fetch)


async def test_binary_source_is_a_limit_and_drive_paths_never_fetch():
    async def binary(*args):
        raise ValueError('Evidence requires UTF-8 text up to 1 MiB')
    payload = await build_bygone_v2(review(), binary)
    assert not payload['links']
    assert 'UTF-8' in '\n'.join(payload['analyses'][0]['limitations'])
    value = review()
    value.links[0].anchors[0].file = 'C:/file.ts'
    async def forbidden(*args):
        pytest.fail('invalid path must not reach provider')
    payload = await build_bygone_v2(value, forbidden)
    assert not payload['links']


async def test_reviewer_requirement_retains_original_quote(monkeypatch):
    from pr_reviewer.pipeline import map_new_requirement
    monkeypatch.setattr('pr_reviewer.pipeline.save_review', lambda value: None)
    value = review()
    class Backend:
        pass  # no hunks, so no model call is needed
    value = await map_new_requirement(value, 'Preserve the selected comparison', Backend())
    async def fetch(*args):
        return '// header\nexport const a = 2;\n'
    payload = await build_bygone_v2(value, fetch)
    assert payload['requirements'][-1]['sources'][0]['quote'] == 'Preserve the selected comparison'


def test_export_endpoint_is_local_and_maps_provider_failure(monkeypatch):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from pr_reviewer import app as app_mod
    value = review()
    monkeypatch.setattr(app_mod.config, 'load_review', lambda rid: value)
    monkeypatch.setattr(app_mod.config, 'load_config', lambda: {})
    async def fetch(*args):
        return '// header\nexport const a = 2;\n'
    provider = SimpleNamespace(fetch_file=fetch)
    monkeypatch.setattr(app_mod, 'build_providers', lambda config: {'github': provider})
    client = TestClient(app_mod.app)
    response = client.get('/api/reviews/github:example/repo:1/export/bygone/v2')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['decisions'] == []
    async def failed(*args):
        raise httpx.ConnectError('fixture')
    provider.fetch_file = failed
    assert client.get('/api/reviews/github:example/repo:1/export/bygone/v2').status_code == 502
    value.capture = None
    assert client.get('/api/reviews/github:example/repo:1/export/bygone/v2').status_code == 409


async def test_pipeline_persists_ticket_title_quotes_and_capture(monkeypatch):
    from pr_reviewer.models import PRContent, TicketContent
    from pr_reviewer.pipeline import run_review
    from types import SimpleNamespace
    saved = []
    monkeypatch.setattr('pr_reviewer.pipeline.save_review', saved.append)
    value = review()
    value.capture.diff_sha256 = hashlib.sha256(b'').hexdigest()
    value.capture.diff_bytes = 0
    value.pr.title = 'REQ-123'
    class Provider:
        name = 'github'
        async def fetch(self, repo, number):
            return PRContent(**value.pr.model_dump(), capture=value.capture, diff='')
        async def fetch_comments(self, repo, number):
            raise RuntimeError('synthetic outage')
    class Tickets:
        def configured(self):
            return True
        async def fetch(self, key):
            return TicketContent(key=key, source='linear', title='Preserve Case', body='Detailed context')
    class Backend:
        async def structured(self, prompt, schema, **kwargs):
            return {'requirements': [{'text': 'Preserve case', 'source': 'linear:REQ-123', 'quote': 'preserve case'}]}
    result = await run_review(Provider(), 'example/repo', 1, {'linear': Tickets()}, Backend())
    assert result.capture == value.capture and saved == [result]
    async def forbidden(*args):
        pytest.fail('no links to fetch')
    payload = await build_bygone_v2(result, forbidden)
    assert payload['requirements'][0]['sources'][0]['quote'] == 'Preserve Case'
    assert any('discussion was unavailable' in item for item in payload['analyses'][0]['limitations'])


async def test_export_import_interoperability(tmp_path):
    import os
    import json
    import subprocess
    from pathlib import Path
    checkout = os.environ.get('BYGONE_CHECKOUT')
    if not checkout:
        pytest.skip('Set BYGONE_CHECKOUT to a compiled Bygone checkout for cross-tool validation')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
    source = tmp_path / 'a.ts'
    source.write_bytes('// 😀\r\nexport const a = 1;\r\n'.encode())
    git('add', '.'); git('commit', '-qm', 'base'); base = git('rev-parse', 'HEAD')
    source.write_bytes('// 😀\r\nexport const a = 2;\r\n'.encode())
    git('add', '.'); git('commit', '-qm', 'head'); head = git('rev-parse', 'HEAD')
    value = review()
    value.capture.base_oid = value.capture.merge_base_oid = base
    value.capture.head_oid = head
    async def fetch(repo, revision, path):
        assert revision == head
        return subprocess.check_output(['git', '-C', str(tmp_path), 'show', f'{revision}:{path}']).decode()
    payload = await build_bygone_v2(value, fetch)
    artifact = tmp_path / 'export.json'
    artifact.write_text(json.dumps(payload))
    imported = tmp_path / 'imported.json'
    cli = str(Path(checkout) / 'bin/bygone.js')
    subprocess.run(['node', cli, '-C', str(tmp_path), 'evidence', 'import', str(artifact), '--output', str(imported)], check=True, capture_output=True)
    result = subprocess.check_output(['node', cli, '-C', str(tmp_path), 'evidence', 'inspect', str(imported)], text=True)
    assert json.loads(result)['report']['requirements'][0]['links'][0]['resolution']['status'] == 'valid'
    tour = tmp_path / 'evidence.bygone'
    subprocess.run(['node', cli, '-C', str(tmp_path), 'evidence', 'tour', str(imported), '--output', str(tour)], check=True, capture_output=True)
    assert json.loads(tour.read_text())['range'] == {'base': base, 'head': head}


async def test_export_preserves_unexplained_changes_and_overflow_as_limitations():
    from pr_reviewer.models import UnexplainedChange, Anchor
    value = review()
    value.unexplained = [UnexplainedChange(id='U1', label='Unrelated behavior', mechanism='Changes the fallback', anchors=[Anchor(file='a.ts', start=3, end=3)], hunk_ids=['H2'])]
    value.overflow = {'requirements': 2}
    async def fetch(*args):
        return '// header\nexport const a = 2;\nsideEffect();\n'
    payload = await build_bygone_v2(value, fetch)
    limitations = payload['analyses'][0]['limitations']
    assert any('U1: Unrelated behavior' in item and 'a.ts:3-3' in item and 'H2' in item for item in limitations)
    assert any('omitted 2 items from requirements' in item for item in limitations)


async def test_discussion_http_failure_is_not_an_empty_discussion(monkeypatch):
    import httpx
    from pr_reviewer.providers.github import GitHubProvider
    async def get(self, url, **kwargs):
        return httpx.Response(503, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx.AsyncClient, 'get', get)
    with pytest.raises(httpx.HTTPStatusError):
        await GitHubProvider().fetch_comments('example/repo', 1)


@pytest.mark.parametrize('outside', [False, True])
async def test_pipeline_uses_same_bounded_discussion_for_prompt_quotes_and_export(monkeypatch, outside):
    from pr_reviewer.models import PRContent
    from pr_reviewer.pipeline import run_review
    monkeypatch.setattr('pr_reviewer.pipeline.save_review', lambda value: None)
    value = review()
    value.capture.diff_sha256 = hashlib.sha256(b'').hexdigest()
    value.capture.diff_bytes = 0
    discussion = 'Visible requirement. ' + 'x' * 8000 + ' Hidden requirement.'
    quote = 'Hidden requirement.' if outside else 'Visible requirement.'
    class Provider:
        name = 'github'
        async def fetch(self, repo, number):
            return PRContent(**value.pr.model_dump(), capture=value.capture, diff='')
        async def fetch_comments(self, repo, number):
            return [{'author': 'fixture', 'body': discussion}]
    class Backend:
        async def structured(self, prompt, schema, **kwargs):
            if 'requirements' in schema.get('properties', {}):
                assert 'Visible requirement.' in prompt
                assert 'Hidden requirement.' not in prompt
            return {'requirements': [{'text': quote, 'source': 'pr-discussion', 'quote': quote}]}
    result = await run_review(Provider(), 'example/repo', 1, {}, Backend())
    source = next(s for s in result.sources if s.tag == 'pr-discussion')
    assert source.text == ('@fixture: ' + discussion)[:8000]
    assert any('truncated to 8000' in item for item in result.capture.limitations)
    async def forbidden(*args):
        pytest.fail('no links to fetch')
    if outside:
        assert result.requirements[0].quote == ''
        with pytest.raises(ValueError, match='quotation unavailable'):
            await build_bygone_v2(result, forbidden)
    else:
        payload = await build_bygone_v2(result, forbidden)
        assert payload['requirements'][0]['sources'][0]['quote'] == quote
