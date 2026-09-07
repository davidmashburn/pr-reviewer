"""Local config + review persistence. Tokens never leave this machine."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .models import Review

DATA_DIR = Path.home() / ".pr-reviewer"
CONFIG_PATH = DATA_DIR / "config.json"
REVIEWS_DIR = DATA_DIR / "reviews"

_lock = threading.Lock()

DEFAULT_CONFIG: dict[str, Any] = {
    "github": {"token": "", "repos": []},
    "bitbucket": {"username": "", "app_password": "", "repos": []},
    # api_key is the manual fallback; the oauth_* fields are written by the
    # browser-auth flow (PKCE) and rotate automatically (24h access tokens).
    "linear": {"api_key": "", "client_id": "", "oauth_access_token": "",
               "oauth_refresh_token": "", "oauth_expires_at": ""},
    "jira": {"site_url": "", "email": "", "api_token": ""},
    # skills_dir: where to discover user skills ("" = ~/.claude/skills).
    # review_skill: skill slash-command for the code-review pass ("" = built-in /code-review).
    "claude": {"model": "sonnet", "skills_dir": "", "review_skill": ""},
    # Auto-review watcher. Scope is fixed by design: ONLY PRs where the user's
    # review is requested / they are assigned — never their own, never all PRs.
    # `since` is stamped when enabling so pre-existing PRs are never backfilled.
    "auto_review": {"enabled": False, "poll_seconds": 120, "max_per_hour": 3, "since": ""},
    # Custom review: free-text standing instructions injected into the review
    # prompts, plus the output template (summary section order/visibility and
    # findings grouping). All UI-configurable.
    "custom_review": {
        "instructions": "",
        "findings_group_by": "severity",
        "sections": ["net_effect", "requirements", "unexplained", "architecture", "findings", "files"],
        # Version of the section list. Defaults to the LEGACY version on purpose:
        # a saved config without the key predates every migration, and
        # load_config fills defaults before migrating.
        "sections_v": 1,
    },
    "pins": [],  # individually added PRs: "provider:owner/repo:number"
}


def change_pin(rid: str, add: bool) -> list[str]:
    cfg = load_config()
    pins: list[str] = cfg.get("pins", [])
    if add and rid not in pins:
        pins.append(rid)
    if not add and rid in pins:
        pins.remove(rid)
    cfg["pins"] = pins
    save_config(cfg)
    return pins


def load_config() -> dict[str, Any]:
    with _lock:
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    for key, val in DEFAULT_CONFIG.items():
        cfg.setdefault(key, json.loads(json.dumps(val)))
        if isinstance(val, dict):
            for k2, v2 in val.items():
                cfg[key].setdefault(k2, v2)
    _migrate_sections(cfg["custom_review"])
    return cfg


# Sections added after a user saved their own order/visibility would otherwise
# stay invisible forever; each version step inserts its new key once.
_SECTION_MIGRATIONS: list[tuple[int, str, str]] = [
    (2, "architecture", "unexplained"),  # (version, new key, insert after)
]


def _migrate_sections(cr: dict[str, Any]) -> None:
    have = int(cr.get("sections_v") or 1)
    sections: list[str] = list(cr.get("sections") or [])
    for version, key, after in _SECTION_MIGRATIONS:
        if have >= version or key in sections:
            continue
        idx = sections.index(after) + 1 if after in sections else len(sections)
        sections.insert(idx, key)
    cr["sections"] = sections
    cr["sections_v"] = max(have, *(v for v, _, _ in _SECTION_MIGRATIONS))


def reconcile_client_sections(values: dict[str, Any]) -> dict[str, Any]:
    """Apply a client's `sections` write without letting a stale client erase
    keys it never knew about. A page loaded before a new section shipped sends
    the old list (and no `sections_v`); re-applying the migrations newer than
    the version the client declares restores what it could not have unchecked.
    A current client that deliberately hides a key sends the current version,
    so its choice sticks."""
    if "sections" not in values:
        return values
    cr = {"sections": list(values["sections"]), "sections_v": int(values.get("sections_v") or 1)}
    _migrate_sections(cr)
    return {**values, **cr}


def save_config(cfg: dict[str, Any]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def update_section(section: str, values: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    cfg.setdefault(section, {})
    cfg[section].update(values)
    save_config(cfg)
    return cfg


def _review_path(rid: str) -> Path:
    safe = rid.replace("/", "__").replace(":", "--")
    return REVIEWS_DIR / f"{safe}.json"


def save_review(review: Review) -> None:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = _review_path(review.id)
    tmp = path.with_suffix(".tmp")  # atomic: never leave a half-written review
    tmp.write_text(review.model_dump_json(indent=1))
    tmp.replace(path)


def delete_review(rid: str) -> bool:
    path = _review_path(rid)
    if path.exists():
        path.unlink()
        return True
    return False


def load_review(rid: str) -> Review | None:
    path = _review_path(rid)
    if not path.exists():
        return None
    return Review.model_validate_json(path.read_text())


def all_reviews() -> list[Review]:
    if not REVIEWS_DIR.exists():
        return []
    out = []
    for p in sorted(REVIEWS_DIR.glob("*.json")):
        try:
            out.append(Review.model_validate_json(p.read_text()))
        except Exception:
            continue
    return out
