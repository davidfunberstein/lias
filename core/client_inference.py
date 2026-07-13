"""
Client inference from existing manifest CSVs.

Logic:
- Scan all CSV files under root_dir
- Find rows where the "מייצג" column contains the lawyer name (case-insensitive partial match)
- From the CSV's parent folder name, extract party names (split by " - ", skip first element = procedure)
- Build frequency map: party_name → count of cases where this party appears alongside lawyer
- The party with highest count = the client
- Also collect all "מייצג" values that are SIMILAR but NOT matching for mismatch warnings
"""
from __future__ import annotations
import csv
import re
from collections import Counter
from pathlib import Path


def _normalize(name: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _extract_parties_from_folder_name(folder_name: str) -> list[str]:
    """
    Parse party names from a case folder name.
    Format: "{procedure} - {party1} - {party2}" or "{procedure} - {party1}"
    Returns list of party name strings (everything after the first segment).
    """
    parts = [p.strip() for p in folder_name.split(" - ")]
    # First part is procedure (e.g. "אישות", "נפשות", etc.)
    return parts[1:] if len(parts) > 1 else []


def _similar_but_not_matching(value: str, lawyer_name: str) -> bool:
    """
    Return True if value looks like it could be the lawyer but doesn't match.
    Heuristic: shares >60% of words, but doesn't contain the lawyer name as substring.
    """
    norm_val = _normalize(value)
    norm_lawyer = _normalize(lawyer_name)
    if norm_lawyer in norm_val:
        return False  # It matches — not a mismatch
    lawyer_words = set(norm_lawyer.split())
    val_words = set(norm_val.split())
    overlap = lawyer_words & val_words
    if len(lawyer_words) == 0:
        return False
    return len(overlap) / len(lawyer_words) >= 0.5


def infer_client_name(root_dir: Path, lawyer_name: str) -> tuple[str | None, list[str]]:
    """
    Scan all CSV files under root_dir.
    Returns:
        (client_name, mismatches)
        client_name: the inferred client name, or None if not enough data
        mismatches: list of מייצג values that look similar to lawyer_name but don't match
    """
    if not lawyer_name or not root_dir.exists():
        return None, []

    party_counter: Counter = Counter()
    all_rep_values: set[str] = set()

    for csv_path in root_dir.rglob("*.csv"):
        # Skip batch_progress and summary files
        if "batch_progress" in csv_path.name or "summary" in csv_path.name or "sync_history" in csv_path.name:
            continue
        try:
            with csv_path.open(encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    continue
                if "מייצג" not in reader.fieldnames:
                    continue
                rows = list(reader)

            # Check if this case has the lawyer as representative
            lawyer_found = any(
                lawyer_name.lower() in (row.get("מייצג") or "").lower()
                for row in rows
            )

            # Collect all מייצג values for mismatch detection
            for row in rows:
                rep = (row.get("מייצג") or "").strip()
                if rep:
                    all_rep_values.add(rep)

            if not lawyer_found:
                continue

            # Extract parties from the case folder name (csv_path.parent.name)
            folder_name = csv_path.parent.name
            parties = _extract_parties_from_folder_name(folder_name)
            for party in parties:
                if party:
                    party_counter[party] += 1

        except Exception:
            continue

    # Mismatch detection: מייצג values similar to lawyer_name but not matching
    mismatches = [
        v for v in all_rep_values
        if _similar_but_not_matching(v, lawyer_name)
    ]

    if not party_counter:
        return None, mismatches

    # Client = most frequent party across cases where lawyer appears
    client_name, _count = party_counter.most_common(1)[0]
    # Only return if seen in at least 1 case (could require 2+ for confidence)
    return client_name, mismatches


def get_client_base_dir(root_dir: Path, client_name: str) -> Path:
    """Return root_dir / client_name. Creates the directory."""
    client_dir = root_dir / client_name
    client_dir.mkdir(parents=True, exist_ok=True)
    return client_dir
