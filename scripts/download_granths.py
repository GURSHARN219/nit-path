#!/usr/bin/env python3
"""
Download BaniDB Granth pages (Angs) as JSON chunks for NitPath.

This imports Granth pages only; it does not generate individual Bani JSON files.
Requires: python -m pip install requests

Examples:
  python scripts/download_granths.py --granth sggs --angs 1 2 3 --dry-run
  python scripts/download_granths.py --granth dasam --start-ang 1 --end-ang 10
  python scripts/download_granths.py --granth sggs --all
  python scripts/download_granths.py --granth dasam --all

BaniDB v2 response shape confirmed from a sample:
  { "source": {...}, "count": N, "navigation": {...}, "page": [verse, ...] }

The runtime JSON contains normalized fields only (no duplicated `sourceRecord`).
It preserves source IDs and update timestamps, and does not infer composition boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://api.banidb.com/v2/angs"
SOURCES = {
    "sggs": {"code": "G", "folder": "guru_granth_sahib",
             "name": "Sri Guru Granth Sahib Ji"},
    "dasam": {"code": "D", "folder": "dasam_granth",
              "name": "Dasam Bani / Sri Dasam Granth Sahib"},
}
USER_AGENT = "NitPathGranthImporter/1.2"


class AngNotFound(RuntimeError):
    """Raised when BaniDB confirms that an Ang does not exist (HTTP 404)."""


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get_payload(session: requests.Session, ang: int, source_code: str,
                timeout: float, retries: int) -> dict[str, Any]:
    url = f"{API_BASE}/{ang}/{source_code}"
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = session.get(
                url, timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if response.status_code == 404:
                raise AngNotFound(f"BaniDB has no Ang {ang} for source {source_code} (404).")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object, got {type(payload).__name__}")
            return payload
        except AngNotFound:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 12))
    raise RuntimeError(f"Failed to fetch Ang {ang} from {url}: {last_error}")


def find_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """BaniDB v2 places verse records in the top-level `page` array."""
    lines = payload.get("page")
    if not isinstance(lines, list):
        raise ValueError(
            "Unexpected BaniDB response: top-level 'page' is not a list. "
            f"Keys received: {list(payload.keys())}"
        )
    if not all(isinstance(item, dict) for item in lines):
        raise ValueError("BaniDB 'page' array contains non-object entries.")
    return lines


def nested_text(value: Any) -> str:
    """Extract a text leaf from varying BaniDB translation objects."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # Prefer conventional text-bearing keys, then recursively inspect values.
        for key in ("unicode", "text", "translation", "steek", "value"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
        for child in value.values():
            found = nested_text(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = nested_text(child)
            if found:
                return found
    return ""


def translation_for(record: dict[str, Any], language: str, preferred: tuple[str, ...]) -> str:
    translations = record.get("translation")
    if not isinstance(translations, dict):
        return ""
    language_data = translations.get(language)
    if not isinstance(language_data, dict):
        return nested_text(language_data)
    for key in preferred:
        if key in language_data:
            text = nested_text(language_data[key])
            if text:
                return text
    return nested_text(language_data)


def normalize_line(raw: dict[str, Any], ang: int, index: int, granth_id: str) -> dict[str, Any]:
    verse = raw.get("verse") if isinstance(raw.get("verse"), dict) else {}
    transliteration = raw.get("transliteration") if isinstance(raw.get("transliteration"), dict) else {}
    gurmukhi = verse.get("unicode") or verse.get("gurmukhi") or ""
    if not isinstance(gurmukhi, str) or not gurmukhi.strip():
        raise ValueError(f"Ang {ang}, line {index}: missing verse.unicode/gurmukhi")

    en_translit = transliteration.get("english") or transliteration.get("en") or ""
    hi_translit = transliteration.get("hindi") or transliteration.get("hi") or ""
    raag_data = raw.get("raag") if isinstance(raw.get("raag"), dict) else {}
    raag = raag_data.get("english") or raag_data.get("unicode") or ""
    return {
        "id": f"{granth_id}_ang_{ang:04d}_line_{index:04d}",
        "lineIndex": index,
        "gurmukhi": gurmukhi.strip(),
        "transliteration": en_translit.strip() if isinstance(en_translit, str) else "",
        "translation": translation_for(raw, "en", ("bdb", "ms", "ssk")),
        "punjabiViakhya": translation_for(raw, "pu", ("ss", "ft", "bdb", "ms")),
        "source": "SGGS" if granth_id == "sggs" else "DASAM_GRANTH",
        "ang": ang,
        "hindi": translation_for(raw, "hi", ("ss", "sts")),
        "hindiTransliteration": hi_translit.strip() if isinstance(hi_translit, str) else "",
        "sourceLineId": str(raw.get("verseId", "")),
        "sourceShabadId": str(raw.get("shabadId", "")),
        "sourceUpdated": raw.get("updated", ""),
        "raag": raag.strip() if isinstance(raag, str) else "",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download BaniDB Granth Ang JSON chunks.")
    parser.add_argument("--granth", choices=sorted(SOURCES), required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--angs", type=int, nargs="+", help="Specific Ang/page numbers")
    group.add_argument("--start-ang", type=int, help="First Ang, inclusive")
    group.add_argument("--all", action="store_true", help="Discover the available Ang range from BaniDB and download it")
    parser.add_argument("--end-ang", type=int, help="Last Ang, inclusive; required with --start-ang")
    parser.add_argument("--output", type=Path, default=Path("content/granths"))
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.end_ang is not None:
        print("ERROR: --end-ang cannot be combined with --all.", file=sys.stderr)
        return 2
    if args.angs is not None and args.end_ang is not None:
        print("ERROR: --end-ang can only be used with --start-ang.", file=sys.stderr)
        return 2
    if not args.all and (args.start_ang is None) != (args.end_ang is None):
        print("ERROR: --start-ang and --end-ang must be used together.", file=sys.stderr)
        return 2
    if not args.all and args.start_ang is not None and args.end_ang < args.start_ang:
        print("ERROR: --end-ang must be greater than or equal to --start-ang.", file=sys.stderr)
        return 2
    cfg = SOURCES[args.granth]
    if args.all:
        # BaniDB's navigation object does not consistently expose a total-page
        # field. Walk sequentially and stop only when the API returns HTTP 404.
        # A high safety ceiling prevents an accidental unbounded loop.
        angs = list(range(1, 10001))
        print(f"Discovering available Angs for {args.granth} by sequential API requests.")
    else:
        angs = args.angs if args.angs is not None else list(range(args.start_ang, args.end_ang + 1))
    if not angs or any(a < 1 for a in angs) or len(set(angs)) != len(angs):
        print("ERROR: Ang numbers must be positive and unique.", file=sys.stderr)
        return 2

    target = args.output / cfg["folder"]
    ang_dir = target / "angs"
    manifest_path = target / "manifest.json"
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "granthId": args.granth,
        "displayName": cfg["name"],
        "provider": "BaniDB",
        "apiBase": API_BASE,
        "sourceCode": cfg["code"],
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "storage": "one JSON file per Ang/page",
        "compositionBoundariesIncluded": False,
        "angs": {},
    }
    if manifest_path.exists() and not args.dry_run:
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if old.get("granthId") == args.granth:
                manifest["angs"] = old.get("angs", {})
        except (OSError, json.JSONDecodeError):
            print(f"WARNING: Could not read existing manifest: {manifest_path}")

    session = requests.Session()
    successes = failures = skipped = 0
    for ang in angs:
        output_file = ang_dir / f"{ang:04d}.json"
        if output_file.exists() and not args.force and not args.dry_run:
            print(f"SKIP Ang {ang}: already exists (use --force to replace)")
            skipped += 1
            continue
        try:
            payload = get_payload(session, ang, cfg["code"], args.timeout, args.retries)
            api_source = payload.get("source")
            if isinstance(api_source, dict):
                returned_code = api_source.get("sourceId")
                if returned_code and returned_code != cfg["code"]:
                    raise ValueError(
                        f"Ang {ang}: requested source {cfg['code']} but API returned {returned_code}"
                    )
            raw_lines = find_lines(payload)
            lines = [normalize_line(raw, ang, i, args.granth) for i, raw in enumerate(raw_lines, 1)]
            if not lines:
                raise ValueError(f"Ang {ang}: API returned an empty page")
            document = {
                "schemaVersion": 1,
                "granthId": args.granth,
                "ang": ang,
                "lineCount": len(lines),
                "source": "BaniDB",
                "lines": lines,
            }
            encoded = json_bytes(document)
            digest = sha256(encoded)
            print(f"OK Ang {ang}: {len(lines)} lines; sha256={digest}")
            if not args.dry_run:
                ang_dir.mkdir(parents=True, exist_ok=True)
                output_file.write_bytes(encoded)
                manifest["angs"][str(ang)] = {
                    "file": f"angs/{ang:04d}.json",
                    "lineCount": len(lines),
                    "sha256": digest,
                    "verified": False,
                }
            successes += 1
        except AngNotFound as exc:
            if args.all:
                print(f"Reached end of {args.granth} at Ang {ang - 1}: {exc}")
                break
            failures += 1
            print(f"ERROR Ang {ang}: {exc}", file=sys.stderr)
        except Exception as exc:
            failures += 1
            print(f"ERROR Ang {ang}: {exc}", file=sys.stderr)
        if args.delay > 0:
            time.sleep(args.delay)

    if not args.dry_run:
        target.mkdir(parents=True, exist_ok=True)
        manifest["generatedAtUtc"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_bytes(json_bytes(manifest))
        print(f"Manifest written: {manifest_path}")
    print(f"Finished: {successes} succeeded, {failures} failed, {skipped} skipped.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
