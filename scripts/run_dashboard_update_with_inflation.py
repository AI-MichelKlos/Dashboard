#!/usr/bin/env python3
"""Run the dashboard update and refresh inflation directly from PRIS01."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import run_dashboard_update as updater


_original_refresh_statbank = updater.refresh_statbank


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


updater.refresh_statbank = refresh_statbank


if __name__ == "__main__":
    raise SystemExit(updater.main())
