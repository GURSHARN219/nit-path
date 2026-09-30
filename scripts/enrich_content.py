#!/usr/bin/env python3
"""Enrich BaniDto JSON files with BaniDB Hindi and source metadata.

Examples:
  python scripts/enrich_content.py --dry-run
  python scripts/enrich_content.py --output content --require-complete
  python scripts/enrich_content.py --ids JAPJI_SAHIB ANAND_SAHIB --output content

The script never overwrites input files. It writes enriched files and a manifest
under --output. BaniDB currently provides exact SGGS Ang data; compositions
outside SGGS are reported as unmatched unless another source is added.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "https://api.banidb.com/v2/angs/"
DEFAULT_INPUT = Path("app/src/main/assets/bundled")
DEFAULT_OUTPUT = Path("content")
DEFAULT_IDS = (
    "JAPJI_SAHIB",
    "JAAP_SAHIB",
    "TAV_PRASAD_SAVAIYE",
    "CHAUPAI_SAHIB",
    "ANAND_SAHIB",
    "REHRAS_SAHIB",
    "KIRTAN_SOHILA",
    "SIDH_GOSHT",
    "RAAG_MALA",
    "SALOK_MAHALLA_9",
    "AKAL_USTAT",
)

BANI_SOURCES = {
    "AKAL_USTAT": "D",
    "ANAND_SAHIB": "G",
    "ARDAS": "R",
    "ASA_KI_VAAR": "G",
    "CHANDI_DI_VAAR": "D",
    "CHAUPAI_SAHIB": "D",
    "DASAM_GRANTH_COMPOSITIONS": "D",
    "DUKH_BHANJANI_SAHIB": "G",
    "JAAP_SAHIB": "D",
    "JAPJI_SAHIB": "G",
    "KIRTAN_SOHILA": "G",
    "RAAG_MALA": "G",
    "REHRAS_SAHIB": "G",
    "SALOK_MAHALLA_9": "G",
    "SHABAD_HAZARE": "G",
    "SHASTAR_NAAM_MALA_DASAM": "D",
    "SIDH_GOSHT": "G",
    "SRI_GURU_GRANTH_SAHIB": "G",
    "SUKHMANI_SAHIB": "G",
    "TAV_PRASAD_SAVAIYE": "D",
}

SOURCE_NAMES = {
    "G": "Sri Guru Granth Sahib Ji",
    "D": "Sri Dasam Granth Sahib",
    "B": "Bhai Gurdas Ji Vaaran",
    "A": "Amrit Keertan",
    "S": "Bhai Gurdas Singh Ji Vaaran",
    "N": "Bhai Nand Lal Ji writings",
    "R": "Rehatnamas and Panthic Sources",
}

# SGGS-based daily paths. These ranges are deliberately explicit so the tool
# never guesses Angs for a composition that is not in SGGS.
SGGS_RANGES = {
    "JAPJI_SAHIB": range(1, 9),
    "ANAND_SAHIB": range(917, 923),
    "REHRAS_SAHIB": range(8, 13),
    "KIRTAN_SOHILA": range(12, 14),
}

BANI_RANGES = {
    "JAPJI_SAHIB": range(1, 9),
    "ANAND_SAHIB": range(917, 923),
    "REHRAS_SAHIB": range(8, 13),
    "KIRTAN_SOHILA": range(12, 14),
    "SIDH_GOSHT": range(938, 947),
    "RAAG_MALA": range(1429, 1431),
    "SALOK_MAHALLA_9": range(1426, 1430),
    "SUKHMANI_SAHIB": range(262, 297),
}


def normalize_gurmukhi(value: str) -> str:
    """Normalize whitespace and punctuation for source-line matching."""
    return re.sub(r"[^\u0a00-\u0a7f0-9]", "", value or "")


def first_value(values: Any, *keys: str) -> str:
    if not isinstance(values, dict):
        return ""
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in values.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def fetch_json(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "Nit-Path-content-enricher/1.0"})
            with urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
            return json.loads(payload)
        except (HTTPError, URLError, TimeoutError, http.client.IncompleteRead, json.JSONDecodeError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Unable to fetch {url}: {last_error}")


def fetch_ang_pages(
    ang: int,
    source: str,
    cache: dict[tuple[str, int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    cache_key = (source, ang)
    if cache_key in cache:
        return cache[cache_key]
    payload = fetch_json(f"{API_BASE}{ang}/{source}")
    pages = payload.get("page")
    if not isinstance(pages, list):
        raise ValueError(f"BaniDB source {source} Ang {ang} has no page array")
    cache[cache_key] = [item for item in pages if isinstance(item, dict)]
    time.sleep(0.05)
    return cache[cache_key]


def build_lookup(
    bani_id: str,
    cache: dict[tuple[str, int], list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    source = BANI_SOURCES.get(bani_id)
    ang_range = BANI_RANGES.get(bani_id)
    if source is None or ang_range is None:
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for ang in ang_range:
        for verse in fetch_ang_pages(ang, source, cache):
            text = verse.get("verse") or {}
            key = normalize_gurmukhi(text.get("unicode", ""))
            if key:
                lookup.setdefault(key, {"ang": ang, "source": source, "verse": verse})
    return lookup


def enrich_line(line: dict[str, Any], match: dict[str, Any]) -> bool:
    verse = match["verse"]
    translation = verse.get("translation") or {}
    transliteration = verse.get("transliteration") or {}
    english = first_value(translation.get("en"), "bdb", "ms", "ssk")
    punjabi = ""
    punjabi_values = translation.get("pu")
    if isinstance(punjabi_values, dict):
        for provider in ("ms", "bdb", "ss", "ft"):
            provider_value = punjabi_values.get(provider)
            if isinstance(provider_value, dict):
                punjabi = provider_value.get("unicode") or ""
                if punjabi.strip():
                    break
    hindi = first_value(translation.get("hi"), "ss", "sts")
    hindi_transliteration = first_value(transliteration, "hindi", "hi")

    line["ang"] = match["ang"]
    line["source"] = SOURCE_NAMES.get(match["source"], line.get("source", ""))
    if english and not line.get("translation"):
        line["translation"] = english
    if punjabi and not line.get("punjabiViakhya"):
        line["punjabiViakhya"] = punjabi
    if hindi:
        line["hindi"] = hindi
    if hindi_transliteration:
        line["hindiTransliteration"] = hindi_transliteration
    return bool(hindi and hindi_transliteration)


def validate_document(document: dict[str, Any], path: Path) -> None:
    lines = document.get("lines")
    if not isinstance(lines, list):
        raise ValueError(f"{path}: lines must be an array")
    if document.get("lineCount") != len(lines):
        raise ValueError(f"{path}: lineCount does not match lines length")
    indexes = [line.get("lineIndex") for line in lines]
    expected = list(range(1, len(lines) + 1))
    if indexes != expected:
        raise ValueError(f"{path}: lineIndex values are not sequential")
    for index, line in enumerate(lines, start=1):
        if not line.get("gurmukhi", "").strip():
            raise ValueError(f"{path}: line {index} has empty Gurmukhi")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def process_file(
    input_path: Path,
    output_path: Path,
    cache: dict[tuple[str, int], list[dict[str, Any]]],
) -> tuple[int, int]:
    document = json.loads(input_path.read_text(encoding="utf-8"))
    validate_document(document, input_path)
    lookup = build_lookup(document["baniId"], cache)
    enriched = 0
    unmatched = 0
    for line in document["lines"]:
        line.setdefault("hindi", "")
        line.setdefault("hindiTransliteration", "")
        key = normalize_gurmukhi(line.get("gurmukhi", ""))
        match = lookup.get(key)
        if match and enrich_line(line, match):
            enriched += 1
        else:
            unmatched += 1
    document["contentVersion"] = max(int(document.get("contentVersion", 1)), 3)
    document["enrichment"] = {
        "source": "BaniDB v2 Ang API",
        "hindiFields": ["hindi", "hindiTransliteration"],
        "matchedLines": enriched,
        "unmatchedLines": unmatched,
    }
    validate_document(document, input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return enriched, unmatched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ids", nargs="+", default=None)
    parser.add_argument(
        "--all-bundled",
        action="store_true",
        help="Process every JSON file in the input folder except manifest.json.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()

    if args.all_bundled:
        ids = sorted(path.stem for path in args.input.glob("*.json") if path.name != "manifest.json")
    elif args.ids:
        ids = args.ids
    else:
        ids = list(DEFAULT_IDS)

    cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
    manifest: list[dict[str, Any]] = []
    failed = False

    for bani_id in ids:
        input_path = args.input / f"{bani_id}.json"
        output_path = args.output / f"{bani_id}.json"
        if not input_path.exists():
            print(f"ERROR: missing {input_path}", file=sys.stderr)
            failed = True
            continue
        try:
            if args.dry_run:
                document = json.loads(input_path.read_text(encoding="utf-8"))
                validate_document(document, input_path)
                lookup = build_lookup(document["baniId"], cache)
                matched = sum(normalize_gurmukhi(line.get("gurmukhi", "")) in lookup for line in document["lines"])
                unmatched = len(document["lines"]) - matched
                output_path_for_manifest = input_path
            else:
                matched, unmatched = process_file(input_path, output_path, cache)
                output_path_for_manifest = output_path
            print(f"{bani_id}: matched={matched}, unmatched={unmatched}")
            if args.require_complete and unmatched:
                failed = True
            if not args.dry_run:
                manifest.append({
                    "id": bani_id,
                    "contentVersion": 3,
                    "lineCount": matched + unmatched,
                    "sizeBytes": output_path_for_manifest.stat().st_size,
                    "sha256": sha256(output_path_for_manifest),
                    "url": f"github://GURSHARN219/nit-path/main/content/{bani_id}.json",
                })
        except (OSError, ValueError, KeyError, HTTPError, URLError, json.JSONDecodeError) as error:
            print(f"ERROR: {bani_id}: {error}", file=sys.stderr)
            failed = True

    if not args.dry_run:
        manifest_path = args.output / "manifest.json"
        manifest_path.write_text(
            json.dumps({"schemaVersion": 1, "catalogVersion": 2, "banis": manifest}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {manifest_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
