#!/usr/bin/env python3
"""
Convert BaniDB Sri Guru Granth Sahib Ji Ang JSON files into NitPath's
normalized per-Ang JSON format.

Supports:
  - Raw BaniDB responses with a top-level "page" array
  - Already-normalized NitPath Ang files with a "lines" array
  - A single JSON file or a directory of JSON files

Example:
  python convert_sggs_json.py --input raw_sggs --output content/granths/guru_granth_sahib
  python convert_sggs_json.py --input full_sggs.json --output content/granths/guru_granth_sahib
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_ID = "SGGS"
GRANTH_ID = "guru_granth_sahib"
GRANTH_NAME = "Sri Guru Granth Sahib Ji"
MAX_ANG = 1430

# Provider preference is explicit and deterministic; adjust only after
# reviewing the source edition and the app's content policy.
ENGLISH_PROVIDERS = ("bdb", "ms", "ssk", "ft", "sttm")
PUNJABI_PROVIDERS = ("ss", "ft", "bdb", "ms", "ssk", "sttm")
HINDI_PROVIDERS = ("ss", "sts", "bdb", "ms")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def first_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def choose_translation(value: Any, providers: tuple[str, ...]) -> str:
    """Select a known provider deterministically; never invent a translation."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for provider in providers:
        candidate = first_text(value.get(provider))
        if candidate:
            return candidate
    # Some BaniDB responses use a direct language field or an unexpected
    # provider. Preserve the first non-empty string, deterministically.
    for key in sorted(value):
        candidate = first_text(value[key])
        if candidate:
            return candidate
    return ""


def get_nested_text(obj: Any, *keys: str) -> str:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return first_text(cur)


def extract_ang(payload: Any, path: Path) -> int:
    if isinstance(payload, dict):
        for key in ("ang", "pageNo", "page_no"):
            value = payload.get(key)
            if isinstance(value, int) and 1 <= value <= MAX_ANG:
                return value
        nav = payload.get("navigation")
        if isinstance(nav, dict):
            for key in ("current", "currentPage", "pageNo", "ang"):
                value = nav.get(key)
                if isinstance(value, int) and 1 <= value <= MAX_ANG:
                    return value
    match = re.search(r"(?<!\d)(\d{1,4})(?!\d)", path.stem)
    if match:
        ang = int(match.group(1))
        if 1 <= ang <= MAX_ANG:
            return ang
    raise ValueError(f"Cannot determine Ang number from {path.name}; use a filename such as 0001.json")


def api_page_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("page")
        if records is None:
            records = payload.get("lines")
    else:
        records = None
    if not isinstance(records, list):
        raise ValueError('Expected a BaniDB top-level "page" array or normalized "lines" array')
    if not all(isinstance(item, dict) for item in records):
        raise ValueError("Every line record must be a JSON object")
    return records


def normalize_line(raw: dict[str, Any], ang: int, index: int) -> dict[str, Any]:
    # Already normalized: retain recognized fields and enforce stable identity.
    if "gurmukhi" in raw and "lineIndex" in raw:
        line = dict(raw)
        line["lineIndex"] = index
        line["ang"] = ang
        line["source"] = SOURCE_ID
        line["id"] = f"sggs_ang_{ang:04d}_line_{index:04d}"
        line.pop("sourceRecord", None)
        return line

    verse = raw.get("verse") if isinstance(raw.get("verse"), dict) else {}
    trans = raw.get("transliteration") if isinstance(raw.get("transliteration"), dict) else {}
    translations = raw.get("translation") if isinstance(raw.get("translation"), dict) else {}
    writer = raw.get("writer") if isinstance(raw.get("writer"), dict) else {}
    raag = raw.get("raag") if isinstance(raw.get("raag"), dict) else {}

    gurmukhi = first_text(verse.get("unicode")) or first_text(verse.get("gurmukhi"))
    if not gurmukhi:
        gurmukhi = first_text(raw.get("gurmukhi"))
    if not gurmukhi:
        raise ValueError(f"Ang {ang}, line {index}: missing Gurmukhi text")

    english_translit = first_text(trans.get("english")) or first_text(trans.get("en"))
    hindi_translit = first_text(trans.get("hindi")) or first_text(trans.get("hi"))

    line = {
        "id": f"sggs_ang_{ang:04d}_line_{index:04d}",
        "lineIndex": index,
        "gurmukhi": gurmukhi,
        "transliteration": english_translit,
        "translation": choose_translation(translations.get("en"), ENGLISH_PROVIDERS),
        "punjabiViakhya": choose_translation(translations.get("pu"), PUNJABI_PROVIDERS),
        "source": SOURCE_ID,
        "ang": ang,
        "hindi": choose_translation(translations.get("hi"), HINDI_PROVIDERS),
        "hindiTransliteration": hindi_translit,
        "sourceLineId": str(raw.get("verseId", raw.get("id", ""))),
        "sourceShabadId": str(raw.get("shabadId", "")),
        "sourceUpdated": first_text(raw.get("updated")),
        "raag": first_text(raag.get("english")) or first_text(raag.get("unicode")),
    }

    # Remove empty optional fields while retaining all required schema keys.
    required = {"id", "lineIndex", "gurmukhi", "source", "ang"}
    return {k: v for k, v in line.items() if k in required or v not in ("", None)}



def convert_full_dataset(path: Path, out_dir: Path, force: bool) -> tuple[list[dict[str, Any]], int]:
    """Split NitPath's monolithic SGGS {baniId, lineCount, lines:[...]} file by Ang."""
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        raise ValueError("Expected a full NitPath JSON object containing a top-level 'lines' array")
    bani_id = str(payload.get("baniId", "")).upper()
    if bani_id not in {"SRI_GURU_GRANTH_SAHIB", "SGGS", "GURU_GRANTH_SAHIB"}:
        raise ValueError(f"Unexpected baniId {bani_id!r}; expected SRI_GURU_GRANTH_SAHIB")
    source_lines = payload["lines"]
    if payload.get("lineCount") is not None and payload["lineCount"] != len(source_lines):
        raise ValueError(f"Declared lineCount {payload['lineCount']} does not match actual lines length {len(source_lines)}")

    grouped: dict[int, list[dict[str, Any]]] = {}
    for pos, raw in enumerate(source_lines, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Line {pos} is not a JSON object")
        try:
            ang = int(raw.get("ang"))
        except (TypeError, ValueError):
            raise ValueError(f"Line {pos} has no valid ang field")
        if not 1 <= ang <= MAX_ANG:
            raise ValueError(f"Line {pos} has Ang {ang} outside valid SGGS range 1–{MAX_ANG}")
        grouped.setdefault(ang, []).append(raw)

    results: list[dict[str, Any]] = []
    for ang in sorted(grouped):
        target = out_dir / "angs" / f"{ang:04d}.json"
        if target.exists() and not force:
            print(f"SKIP Ang {ang}: output exists (use --force to overwrite)")
            continue
        lines: list[dict[str, Any]] = []
        for local_index, raw in enumerate(grouped[ang], start=1):
            line = dict(raw)
            line["id"] = f"sggs_ang_{ang:04d}_line_{local_index:04d}"
            line["lineIndex"] = local_index
            line["ang"] = ang
            line["source"] = SOURCE_ID
            line.pop("sourceRecord", None)
            # Keep the existing source label separately as a Raag only when
            # the source explicitly provides a raag field; do not infer one.
            lines.append(line)
        if len({x["id"] for x in lines}) != len(lines):
            raise ValueError(f"Ang {ang}: duplicate normalized line IDs")
        write_json(target, {
            "granthId": GRANTH_ID,
            "granthName": GRANTH_NAME,
            "source": SOURCE_ID,
            "ang": ang,
            "lineCount": len(lines),
            "contentVersion": str(payload.get("contentVersion", "1")),
            "lines": lines,
        })
        results.append({
            "ang": ang,
            "file": f"angs/{ang:04d}.json",
            "lineCount": len(lines),
            "sha256": sha256_file(target),
        })
        print(f"OK Ang {ang}: {len(lines)} lines -> angs/{ang:04d}.json")
    return results, len(grouped)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def convert_file(path: Path, out_dir: Path) -> dict[str, Any]:
    payload = read_json(path)
    ang = extract_ang(payload, path)
    if not 1 <= ang <= MAX_ANG:
        raise ValueError(f"Ang {ang} outside valid SGGS range 1–{MAX_ANG}")

    records = api_page_records(payload)
    if not records:
        raise ValueError(f"Ang {ang}: empty page/lines array")

    lines = [normalize_line(record, ang, i) for i, record in enumerate(records, start=1)]
    ids = [line["id"] for line in lines]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Ang {ang}: duplicate normalized line IDs")

    # Verify the source's declared page number if available.
    if isinstance(payload, dict):
        declared = payload.get("pageNo")
        if isinstance(declared, int) and declared != ang:
            raise ValueError(f"Filename/derived Ang {ang} conflicts with API pageNo {declared}")

    output = out_dir / "angs" / f"{ang:04d}.json"
    write_json(output, {
        "granthId": GRANTH_ID,
        "granthName": GRANTH_NAME,
        "source": SOURCE_ID,
        "ang": ang,
        "lineCount": len(lines),
        "contentVersion": "1",
        "lines": lines,
    })
    return {
        "ang": ang,
        "file": f"angs/{ang:04d}.json",
        "lineCount": len(lines),
        "sha256": sha256_file(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert raw or full SGGS JSON into NitPath Ang chunks.")
    parser.add_argument("--input", required=True, help="Input JSON file or directory containing JSON files")
    parser.add_argument("--output", required=True, help="Output Granth directory")
    parser.add_argument("--force", action="store_true", help="Overwrite generated Ang files")
    args = parser.parse_args()

    source = Path(args.input)
    out_dir = Path(args.output)
    if not source.exists():
        parser.error(f"Input does not exist: {source}")

    manifest_path = out_dir / "manifest.json"
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    skipped = 0

    if source.is_file():
        try:
            payload = read_json(source)
            if isinstance(payload, dict) and isinstance(payload.get("lines"), list) and "baniId" in payload:
                results, total_angs = convert_full_dataset(source, out_dir, args.force)
                skipped = total_angs - len(results)
                inputs_count = 1
            else:
                result = convert_file(source, out_dir)
                results.append(result)
                inputs_count = 1
        except Exception as exc:
            failures.append(f"{source}: {exc}")
            print(f"ERROR {source}: {exc}", file=sys.stderr)
            inputs_count = 1
    else:
        inputs = sorted(p for p in source.rglob("*.json") if p.is_file())
        if not inputs:
            parser.error(f"No JSON files found in {source}")
        inputs_count = len(inputs)
        for path in inputs:
            try:
                ang = extract_ang(read_json(path), path)
                target = out_dir / "angs" / f"{ang:04d}.json"
                if target.exists() and not args.force:
                    print(f"SKIP Ang {ang}: output exists (use --force to overwrite)")
                    skipped += 1
                    continue
                result = convert_file(path, out_dir)
                results.append(result)
                print(f"OK Ang {result['ang']}: {result['lineCount']} lines -> {result['file']}")
            except Exception as exc:
                failures.append(f"{path}: {exc}")
                print(f"ERROR {path}: {exc}", file=sys.stderr)

    # Merge existing manifest entries so partial conversions preserve prior Ang metadata.
    existing: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            old = read_json(manifest_path)
            for item in old.get("angs", []):
                if isinstance(item, dict) and isinstance(item.get("ang"), int):
                    existing[str(item["ang"])] = item
        except Exception:
            print("WARNING: existing manifest unreadable; rebuilding from this run", file=sys.stderr)

    for item in results:
        existing[str(item["ang"])] = item
    manifest = {
        "granthId": GRANTH_ID,
        "granthName": GRANTH_NAME,
        "source": SOURCE_ID,
        "schemaVersion": 1,
        "contentVersion": "1",
        "expectedAngCount": MAX_ANG,
        "convertedAngCount": len(existing),
        "angs": [existing[k] for k in sorted(existing, key=lambda x: int(x))],
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(manifest_path, manifest)

    print(f"\nConverted this run: {len(results)}; skipped: {skipped}; failed: {len(failures)}")
    print(f"Manifest: {manifest_path}")
    if failures:
        print("\nFailures:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
