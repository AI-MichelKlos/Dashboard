#!/usr/bin/env python3
"""Run the dashboard update and refresh inflation directly from PRIS01."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import run_dashboard_update as updater


_original_refresh_statbank = updater.refresh_statbank
BASE = Path(__file__).resolve().parents[1]
INDEX = BASE / "index.html"


def _dataset(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Uventet JSON-stat-svartype: {type(payload).__name__}")
    dataset = payload.get("dataset", payload)
    if not isinstance(dataset, dict):
        raise RuntimeError("JSON-stat-svaret mangler et datasæt")
    return dataset


def _category_code_by_label(dataset: dict, dimension: str, needle: str) -> str:
    dimensions = dataset.get("dimension")
    if not isinstance(dimensions, dict):
        raise RuntimeError("JSON-stat-svaret mangler dimension")
    dimension_key = next(
        (key for key in dimensions if str(key).lower() == dimension.lower()),
        None,
    )
    if dimension_key is None:
        raise RuntimeError(f"JSON-stat-svaret mangler dimensionen {dimension}")
    category = dimensions[dimension_key].get("category", {})
    labels = category.get("label")
    if not isinstance(labels, dict):
        raise RuntimeError(f"JSON-stat-dimensionen {dimension} mangler kategoritekster")
    needle_lower = needle.lower()
    for code, label in labels.items():
        if needle_lower in str(label).lower():
            return str(code)
    raise RuntimeError(
        f"Kunne ikke finde en {dimension}-kategori, der indeholder teksten {needle!r}"
    )


def _pris01_series(varegruppe: str):
    payload = updater.statbank_get(
        "PRIS01",
        {
            "VAREGR": [varegruppe],
            "ENHED": ["*"],
            "Tid": ["*"],
        },
    )
    dataset = _dataset(payload)
    unit_code = _category_code_by_label(dataset, "ENHED", "samme måned året før")
    labels, series, dataset = updater.jsonstat_series(payload, "ENHED", [unit_code])
    if not labels:
        raise RuntimeError("Statistikbanken returnerede ingen perioder for PRIS01")
    return labels, series[unit_code], dataset, unit_code


def refresh_statbank(data):
    successes, failures = _original_refresh_statbank(data)
    label = "Danmarks Statistik - inflation (PRIS01)"
    try:
        total_labels, total_values, total_dataset, unit_code = _pris01_series("000000")
        core_labels, core_values, core_dataset, core_unit_code = _pris01_series("151")
        if core_unit_code != unit_code:
            raise RuntimeError("PRIS01 returnerede forskellige enhedskoder for inflation og kerneinflation")
        if total_labels != core_labels:
            raise RuntimeError("PRIS01 returnerede forskellige perioder for inflation og kerneinflation")

        data["inflation"] = {
            "labels": total_labels,
            "total": total_values,
            "core": core_values,
        }
        now = datetime.now(ZoneInfo("Europe/Copenhagen"))
        official = data.setdefault("meta", {}).setdefault("officialApi", {})
        official["inflation"] = {
            "source": "Danmarks Statistik",
            "dataset": "PRIS01",
            "filters": {
                "VAREGR": ["000000", "151"],
                "ENHED": [unit_code],
            },
            "unit": "pct. år til år",
            "seasonalAdjustment": "ikke relevant",
            "sourceUpdated": total_dataset.get("updated") or core_dataset.get("updated"),
            "latestPeriod": total_labels[-1],
            "fetchedAt": now.isoformat(timespec="seconds"),
        }
        source_register = data.setdefault("meta", {}).setdefault("sourceRegister", {})
        source_register.setdefault("inflation", {}).update(
            {
                "source": "Danmarks Statistik API",
                "dataset": "PRIS01",
                "unit": "pct. år til år",
                "seasonalAdjustment": "ikke relevant",
            }
        )
        successes.append(label)
        print(
            f"Færdig: {label}. Seneste periode: {total_labels[-1]}. "
            f"Kilden er opdateret: {official['inflation']['sourceUpdated'] or 'ikke oplyst'}.",
            flush=True,
        )
    except Exception as exc:
        failures.append(f"{label}: {exc}")
        print(f"ADVARSEL: {label} blev ikke opdateret: {exc}", flush=True)
    return successes, failures


def tighten_vertical_spacing() -> None:
    """Keep the publication design, but reduce excessive vertical whitespace."""
    html = INDEX.read_text(encoding="utf-8")
    replacements = (
        (
            ".page-header {max-width:1180px; margin:0 auto; padding:72px 20px 54px; border-bottom:1px solid var(--line)}",
            ".page-header {max-width:1180px; margin:0 auto; padding:56px 20px 38px; border-bottom:1px solid var(--line)}",
        ),
        (
            ".page-byline {margin:15px 0 0; color:var(--muted); font-size:.86rem; font-weight:700}",
            ".page-byline {margin:12px 0 0; color:var(--muted); font-size:.86rem; font-weight:700}",
        ),
        (
            ".page-lead {max-width:780px; margin:22px 0 0; color:var(--muted); font-size:1.08rem; line-height:1.7}",
            ".page-lead {max-width:780px; margin:18px 0 0; color:var(--muted); font-size:1.08rem; line-height:1.7}",
        ),
        (
            ".page-updated {display:inline-flex; margin:25px 0 0; padding:6px 9px; align-items:center; border:1px solid var(--line); border-radius:4px; color:var(--muted); background:var(--accent-soft); font-size:.78rem; font-weight:750}",
            ".page-updated {display:inline-flex; margin:18px 0 0; padding:6px 9px; align-items:center; border:1px solid var(--line); border-radius:4px; color:var(--muted); background:var(--accent-soft); font-size:.78rem; font-weight:750}",
        ),
        (
            "max-width:1180px; margin:0 auto; padding:40px 20px 72px; color:var(--ink);",
            "max-width:1180px; margin:0 auto; padding:24px 20px 72px; color:var(--ink);",
        ),
        (
            "#dak-dashboard h2 {font-family:var(--serif); font-size:clamp(1.75rem,2.5vw,2.2rem); font-weight:600; color:var(--ink); margin:58px 0 20px; padding-bottom:10px; border-bottom:1px solid var(--line); letter-spacing:-.01em}",
            "#dak-dashboard h2 {font-family:var(--serif); font-size:clamp(1.75rem,2.5vw,2.2rem); font-weight:600; color:var(--ink); margin:46px 0 18px; padding-bottom:10px; border-bottom:1px solid var(--line); letter-spacing:-.01em}",
        ),
        (
            "#dak-dashboard h2:first-of-type {margin-top:34px}",
            "#dak-dashboard h2:first-of-type {margin-top:22px}",
        ),
        (
            "#dak-dashboard .toolbar {display:flex; flex-wrap:wrap; align-items:center; gap:9px; padding:14px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); margin:0 0 24px}",
            "#dak-dashboard .toolbar {display:flex; flex-wrap:wrap; align-items:center; gap:9px; padding:14px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); margin:0 0 14px}",
        ),
        (
            ".page-header {padding:49px 16px 40px}",
            ".page-header {padding:38px 16px 28px}",
        ),
        (
            "#dak-dashboard {padding:32px 14px 54px}",
            "#dak-dashboard {padding:20px 14px 54px}",
        ),
    )
    for old, new in replacements:
        if old not in html:
            raise RuntimeError(f"Kunne ikke finde forventet designregel: {old[:80]}")
        html = html.replace(old, new, 1)
    INDEX.write_text(html, encoding="utf-8")
    print("Strammede den lodrette afstand i dashboardets layout.", flush=True)


updater.refresh_statbank = refresh_statbank


if __name__ == "__main__":
    result = updater.main()
    if result in (None, 0):
        tighten_vertical_spacing()
    raise SystemExit(result)

# Manual control trigger 2026-08-11
