"""Google Sheets inventory search module using public CSV export."""

import csv
import io
import logging
import os

import requests

logger = logging.getLogger(__name__)


def _fetch_rows() -> list[dict]:
    """Fetch all rows from the public Google Sheet as CSV."""
    sheet_id = os.environ["GOOGLE_SHEETS_ID"]
    gid = os.environ.get("GOOGLE_SHEETS_GID", "0")
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def search_inventory(
    gemstone: str,
    carat_weight_min: float | None = None,
    carat_weight_max: float | None = None,
    pair: bool = False,
    target: float | None = None,
    sort_ascending: bool = False,
) -> list[dict]:
    """Search the inventory sheet and return matching rows as dicts.

    Results are sorted before returning:
    - If target is set: by proximity to target (closest first)
    - If sort_ascending is True: by carat weight ascending
    """
    rows = _fetch_rows()
    results = []
    gemstone_lower = gemstone.strip().lower()

    # Debug: log unique gemstone values to help diagnose mismatches
    unique_gems = set(str(r.get("Gemstone", "")).strip().lower() for r in rows)
    logger.info("Unique gemstone values in sheet: %s", sorted(unique_gems))
    logger.info("Searching for gemstone=%r, pair=%s, carat=[%s, %s]",
                gemstone_lower, pair, carat_weight_min, carat_weight_max)

    for row in rows:
        row_gem = str(row.get("Gemstone", "")).strip().lower()
        if row_gem != gemstone_lower:
            continue

        row_type = str(row.get("Single/Pair", "")).strip().lower()
        expected = "pair" if pair else "single"
        if row_type != expected:
            continue

        try:
            carat = float(row.get("Carat weight", 0))
        except (ValueError, TypeError):
            continue

        if carat_weight_min is not None and carat < carat_weight_min:
            continue
        if carat_weight_max is not None and carat > carat_weight_max:
            continue

        results.append({
            "Gemstone": row.get("Gemstone", ""),
            "Carat weight": carat,
            "Single/Pair": row.get("Single/Pair", ""),
            "Shape": row.get("Shape", ""),
            "Origin": row.get("Origin", ""),
            "Treatment": row.get("Treatment", ""),
            "Color": row.get("Color", ""),
            "Clarity": row.get("Clarity", ""),
            "Price per ct": row.get("Price per ct", ""),
            "Report": row.get("Report", ""),
            "Link": row.get("Link", ""),
            "Photo": row.get("Photo", ""),
            "Video": row.get("Video", ""),
        })

    if target is not None:
        results.sort(key=lambda r: abs(r["Carat weight"] - target))
    elif sort_ascending:
        results.sort(key=lambda r: r["Carat weight"])

    return results
