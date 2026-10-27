"""Built-in plain fabrics for optional fabric slots on multi-fabric garments.

The first slot (lowest sort_order) always needs a fabric the shop uploaded. Any other slot
may be left empty: the backend then sends a plain WHITE or BLACK fabric image in exactly
the position that slot's fabric would take, so image count, order and prompts never change.

Request field `default_fabrics`: {"<slot apply_to>": "white" | "black"} (a JSON string in
multipart forms). An optional slot with no fabric and no choice uses white.

The images ship with the code (assets/default_fabrics/). They are never uploaded to storage
and never saved to a shop's fabrics.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, status

CHOICES = ("white", "black")
FALLBACK_CHOICE = "white"
MIME_TYPE = "image/jpeg"

_DIR = Path(__file__).resolve().parent / "assets" / "default_fabrics"
_cache: dict[str, bytes] = {}
_cache_lock = threading.Lock()


class DefaultFabricMissing(RuntimeError):
    pass


def label(choice: str) -> str:
    return f"Default {choice}"


def path_for(choice: str) -> Path:
    return _DIR / f"{choice}.jpg"


def load(choice: str) -> tuple[bytes, str]:
    """(bytes, mime) for a default fabric, read from disk once and cached."""
    if choice not in CHOICES:
        raise DefaultFabricMissing(f"Unknown default fabric '{choice}'")
    with _cache_lock:
        cached = _cache.get(choice)
    if cached is not None:
        return cached, MIME_TYPE

    path = path_for(choice)
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"[default_fabrics] ERROR: missing default fabric image {path}: {exc}")
        raise DefaultFabricMissing(f"{label(choice)} fabric image is missing on the server ({path.name})") from exc
    if not data:
        print(f"[default_fabrics] ERROR: default fabric image is empty: {path}")
        raise DefaultFabricMissing(f"{label(choice)} fabric image is empty on the server ({path.name})")

    with _cache_lock:
        _cache[choice] = data
    return data, MIME_TYPE


def ensure_available(choices: list[str]) -> None:
    """API side: fail the request (before any credit debit) if a needed image is missing."""
    for choice in dict.fromkeys(choices):
        try:
            load(choice)
        except DefaultFabricMissing as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{exc}. Upload a fabric for this slot instead, or contact support.",
            ) from exc


def parse_choices(raw: Any) -> dict[str, str]:
    """`default_fabrics` from a request -> {casefolded apply_to: choice}. 400 on bad input.
    A blank value means "not chosen" (the slot then falls back to white)."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='default_fabrics must be a JSON object like {"pallu": "black"}',
            ) from exc
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='default_fabrics must be an object like {"pallu": "black"}',
        )

    choices: dict[str, str] = {}
    for key, value in raw.items():
        apply_to = str(key or "").strip()
        if not apply_to:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default_fabrics has an empty slot name",
            )
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        choice = value.strip().lower() if isinstance(value, str) else None
        if choice not in CHOICES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid default fabric '{str(value)[:40]}' for slot '{apply_to[:80]}'. "
                    "Allowed values: white, black"
                ),
            )
        choices[apply_to.casefold()] = choice
    return choices


def _slot_key(slot: dict[str, Any]) -> str:
    return str(slot.get("apply_to") or "").strip().casefold()


def resolve_slot_entries(
    slots: list[dict[str, Any]],
    provided: dict[str, dict[str, Any]],
    choices: dict[str, str],
) -> list[dict[str, Any]]:
    """One entry per slot, in slot (sort) order.

    slots: the garment's fabric slots in sort order. provided: real fabrics keyed by
    casefolded apply_to. choices: from parse_choices. A real fabric beats a default.
    Default entries are {"default_fabric", "apply_to", "slot_label"}."""
    slot_keys = {_slot_key(slot) for slot in slots}
    unknown = [key for key in choices if key not in slot_keys]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"default_fabrics slot '{unknown[0][:80]}' is not configured for this garment type",
        )

    entries: list[dict[str, Any]] = []
    for index, slot in enumerate(slots):
        key = _slot_key(slot)
        if key in provided:
            entries.append(provided[key])
            continue
        if index == 0:
            name = slot.get("label") or slot.get("apply_to")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The first fabric slot '{name}' needs an uploaded fabric",
            )
        entries.append(
            {
                "default_fabric": choices.get(key, FALLBACK_CHOICE),
                "apply_to": str(slot.get("apply_to") or "").strip(),
                "slot_label": slot.get("label"),
            }
        )
    return entries


def default_slots_record(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What generations.default_fabric_slots stores: the default entries and their position
    (0-based, among the fabric images) so the worker can put each image back in its place."""
    return [
        {
            "position": position,
            "apply_to": entry.get("apply_to"),
            "slot_label": entry.get("slot_label"),
            "default_fabric": entry["default_fabric"],
            "label": label(entry["default_fabric"]),
        }
        for position, entry in enumerate(entries)
        if entry.get("default_fabric")
    ]


def merge_into(real_fabrics: list[dict[str, Any]], record: Optional[list[Any]]) -> list[dict[str, Any]]:
    """Worker side: put recorded defaults back between the real fabrics at their positions."""
    merged = list(real_fabrics)
    # An unknown choice is kept: load() then fails the job (and the worker refunds).
    defaults = [item for item in (record or []) if isinstance(item, dict) and item.get("default_fabric")]
    for item in sorted(defaults, key=lambda d: int(d.get("position") or 0)):
        position = max(0, min(int(item.get("position") or 0), len(merged)))
        merged.insert(
            position,
            {
                "default_fabric": item["default_fabric"],
                "apply_to": str(item.get("apply_to") or ""),
            },
        )
    return merged
