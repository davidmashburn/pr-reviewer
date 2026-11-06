# PR Reviewer

Requirements-grounded PR review: maps a PR's stated intent (description + linked
Linear/Jira tickets) onto the actual diff, with machine-validated line anchors.
See `DESIGN.md` for the full design; `mockup/ui-mockup.html` is the UI spec.

## Install as a Mac app

```bash
./packaging/build_app.sh --install
```

Builds `PR Reviewer.app` and copies it to `/Applications` — it then appears in
Launchpad and Spotlight like any other app. Drop `--install` to build into
`build/` without installing.

The app is a native window (WKWebView) wrapping the local server: it starts the
server on launch, picks the first free port from 8712, and stops it on quit.
Requires `uv` and the `claude` CLI; the bundle injects `~/.local/bin` and
`/opt/homebrew/bin` into `PATH` so both resolve when launched from Finder.

- Python env: `~/Library/Application Support/PR Reviewer/venv` (kept outside the
  bundle so `/Applications` stays read-only). First launch creates it.
- Server log: `~/Library/Logs/PR Reviewer.log` — also under **PR Reviewer →
  Open Server Log** (⌘L).
- Data (`config.json`, `reviews/`) stays in `~/.pr-reviewer`, shared with the
  terminal launcher below.

## Run from a terminal instead

Double-click **`start.command`** in Finder (or run it from a terminal). It
starts the server if needed and opens <http://127.0.0.1:8712> in your browser.
Stop it with **`stop.command`**. Server logs: `~/.pr-reviewer/server.log`.

Or run the server in the foreground yourself:

```bash
uv run uvicorn pr_reviewer.app:app --host 127.0.0.1 --port 8712
```

## Setup (in the app's Settings screen)

- **Claude backend** — uses your existing Claude Code subscription login
  (`claude login`); no API key. The card shows install/login state with fix-it
  guidance, plus a model picker and a full schema-check via Re-check.
- **GitHub** — personal access token (`repo` read scope), then add repos to
  populate the Command Center. Public repos work without a token.
- **Bitbucket** — username + app password (pull request read scope).
- **Linear / Jira** — API credentials for ticket-grounded reviews; PR
  description is always used as a source. Linear also falls back to the
  `LINEAR_API_KEY` environment variable when no key or OAuth token is
  configured, so a key can be shared with other tools.

Tokens live in `~/.pr-reviewer/config.json`; reviews in `~/.pr-reviewer/reviews/`.

## Use

Pick a PR in the Command Center (or paste any PR URL) — the pipeline runs
fetch → parse → extract → map → validate and opens the review: split diff with
requirement chips, evidence-backed fulfillment cards, gap/unexplained flags,
net-effect summary, and a mark-verified checklist. PRs with no stated
requirements fall back to explain mode (annotated changes + net effect).

API clients that perform their own defect pass can request only the structured
requirements and flow analysis by posting `{"url":"<PR_URL>",
"include_findings":false}` to `/api/reviews`. Interactive app reviews continue
to include findings by default.

## Tests

```bash
uv run pytest
```

## Bygone evidence export

For newly captured GitHub reviews, **Export Bygone evidence** downloads a pinned
version 2 evidence artifact. The read-only endpoint is
`GET /api/reviews/{review-id}/export/bygone/v2`. Import it in a local checkout with
`bygone evidence import exported.json --output review.json`, then use VS Code's
**Bygone: Open Evidence Review**. Git objects for the captured comparison must be
available locally.

Capture records the immutable base tip, merge base, head, diff digest, metadata
digest, and time before analysis. A metadata race retries once and then fails.
Exports retain saved source quotations and fetch code only at the captured head;
they never reinterpret an old review against today's branch. Invalid anchors
remain explicit limitations, and incomplete quotation provenance blocks export.
The export does not transfer checklist state, discussion, approval, publication,
or test execution claims. These remain owned by their existing systems.

Legacy reviews and Bitbucket reviews without immutable capture remain readable,
but cannot be exported as pinned evidence. GitHub API coverage and source-size
limits are disclosed; a valid link does not establish completeness or fulfillment.

To validate the cross-tool export/import contract against a compiled Bygone checkout:

```sh
BYGONE_CHECKOUT=/path/to/bygone uv run pytest -q tests/test_bygone_export.py
```

The fixture uses local Git commits and mocked provider data, including CRLF and
non-ASCII text. It checks export, import, source validation, and tour generation;
it does not contact GitHub or run a model. The interoperability case is skipped
when `BYGONE_CHECKOUT` is absent. Export rejects divergent captured repository
identity and oversized artifacts; unavailable individual source files are
reported as limitations. Transport/authentication failures return a retryable
provider error instead of a successful artifact.

GitHub evidence reads mode-checked Git blobs rather than dereferencing symlinks.
Both the captured merge-base and head must support a linked path in Bygone:
non-regular, binary, non-UTF-8, or oversized files at either revision omit that
path with an explicit limitation, while other valid links remain importable.
An absent or empty base is supported. Checking both snapshots requires additional
GitHub reads; transport/authentication failures still fail the export for retry.
