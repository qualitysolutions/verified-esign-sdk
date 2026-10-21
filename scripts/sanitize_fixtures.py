#!/usr/bin/env python3
"""Turn a raw recording of live API responses into sanitized test fixtures.

Input: a JSON list of ``{"request", "status", "location", "body"}`` entries (as written by
the live draft test). Output: one file per request under ``tests/fixtures/live/``.

Every personal value is replaced (names, emails, phone numbers, user/company ids, file
owner ids). The script FAILS if any original value survives, so a fixture can never leak
PII into the repository. Raw recordings belong in ``tests/fixtures/raw/`` (gitignored).

Usage: python scripts/sanitize_fixtures.py tests/fixtures/raw/responses.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "live"

NAME_KEYS = {"givenName": "Test", "familyName": "Testsson"}
EMAIL = "signer@example.com"


def collect_pii(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in NAME_KEYS or key in {"email", "telephone"}:
                if isinstance(value, str) and value:
                    found.add(value)
            elif key == "confirmedEmails" and isinstance(value, list):
                found.update(v for v in value if isinstance(v, str))
            elif key == "company" and isinstance(value, str):
                found.add(value)
            elif key == "label" and isinstance(value, str) and "user" in value.lower():
                found.add(value)  # userinfo role labels carry the company / person name
            collect_pii(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_pii(item, found)


def ids_to_replace(entries: list[dict[str, Any]]) -> dict[str, str]:
    """User and company ids appear inside uids/namespaces; map each to a stable fake."""
    text = json.dumps(entries)
    mapping: dict[str, str] = {}
    for n, cid in enumerate(sorted(set(re.findall(r"/companies/([A-Za-z0-9_-]+)", text))), 1):
        mapping[cid] = f"COMPANY{n}"
    for n, uid in enumerate(sorted(set(re.findall(r"/users/([A-Za-z0-9_-]+)", text))), 1):
        mapping[uid] = f"USER{n}"
    return mapping


def scrub(node: Any, ids: dict[str, str]) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in NAME_KEYS and isinstance(value, str):
                out[key] = NAME_KEYS[key]
            elif key == "email" and isinstance(value, str):
                out[key] = EMAIL
            elif key == "confirmedEmails":
                out[key] = [EMAIL]
            elif key == "telephone":
                out[key] = "+46700000000"
            elif key == "metadata" and isinstance(value, dict):
                out[key] = {"language": value.get("language", "sv_SE")}
            elif key == "label" and isinstance(value, str) and "user" in value.lower():
                out[key] = "Example AB admin users." if "admin" in value else "User Testsson Test"
            elif key in {"_id"} or (key == "id" and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{24}", value)):
                out[key] = "0" * 24
            else:
                out[key] = scrub(value, ids)
        return out
    if isinstance(node, list):
        return [scrub(v, ids) for v in node]
    if isinstance(node, str):
        for real, fake in ids.items():
            node = node.replace(real, fake)
        return node
    return node


def name_for(request: str) -> str:
    method, path = request.split(" ", 1)
    path = re.sub(r"/(envelopes|documents|recipients|files)/[^/?]+", r"/\1/ID", path.split("?")[0])
    return f"{method.lower()}{path.replace('/', '_').replace('.', '_')}.json"


def main(src: str) -> int:
    entries = json.loads(Path(src).read_text())
    pii: set[str] = set()
    collect_pii(entries, pii)
    ids = ids_to_replace(entries)
    OUT.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry.get("request", "").startswith("POST /auth"):
            continue  # credentials exchange: never stored
        clean = scrub(entry, ids)
        text = json.dumps(clean, indent=2, ensure_ascii=False)
        leaked = [v for v in pii | set(ids) if v and v in text]
        if leaked:
            print(f"refusing to write {entry['request']}: {len(leaked)} original value(s) survived", file=sys.stderr)
            return 1
        (OUT / name_for(entry["request"])).write_text(text + "\n")
        print("wrote", name_for(entry["request"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
