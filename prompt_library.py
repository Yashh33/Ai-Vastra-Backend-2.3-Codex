"""Shared prompt library: prompt sets ("single", "two"), their versions, and filling blanks.

A library garment's prompts are rendered here once (on save / move) and written into the
garment's existing look_prompt / tryon_prompt / look_tryon_prompt fields. Generation never
reads the library: the worker and try-on code keep reading those garment fields.

Blanks are square-bracket tokens, e.g. [GARMENT]. The runtime placeholders the worker fills
({garment_name}, {fabric_mapping}, {image_count}) use braces and are left untouched.
"""
from __future__ import annotations

import re
from typing import Any, Optional

SET_KEYS = ("single", "two")
SET_LABELS = {"single": "Single fabric", "two": "Two fabrics"}
SET_BLANKS = {
    "single": ("GARMENT",),
    "two": ("DESIGN", "KEEP", "PART1_LABEL", "PART1", "PART2_LABEL", "PART2"),
}
# Which form field each blank comes from (for "fill in ..." messages).
BLANK_SOURCES = {
    "GARMENT": "Name",
    "DESIGN": "Design",
    "KEEP": "Keep",
    "PART1_LABEL": "Part 1 name",
    "PART1": "Part 1 name",
    "PART2_LABEL": "Part 2 name",
    "PART2": "Part 2 name",
}
PROMPT_FIELDS = ("look_prompt", "tryon_prompt", "look_tryon_prompt")
PART2_DEFAULTS = ("white", "black")
PART_NAME_MAX_LENGTH = 40
# Slot apply_to that the slot code only allows on its own (see admin_api fabric slots).
_RESERVED_SLUGS = {"suit_full_body"}


class PromptLibraryError(ValueError):
    """A request the library cannot fulfil; the message is safe to show the admin."""


def set_label(set_key: Optional[str]) -> str:
    return SET_LABELS.get(set_key or "", "Own prompts")


def version_note(set_key: str, version: int, extra: Optional[str] = None) -> str:
    """Note saved with the garment's prompt version, e.g. "Two fabrics · Version 2"."""
    note = f"{set_label(set_key)} · Version {version}"
    extra = (extra or "").strip()
    return f"{note} · {extra}" if extra else note


def check_set_key(set_key: Optional[str]) -> str:
    key = (set_key or "").strip().lower()
    if key not in SET_KEYS:
        raise PromptLibraryError("Prompt set must be 'single' or 'two'")
    return key


# ---------------------------------------------------------------- blanks

def blank_values(set_key: str, garment: dict[str, Any]) -> dict[str, str]:
    """Blank -> text for one garment. garment holds name / design_text / keep_text /
    part1_name / part2_name. Outer whitespace of each value is dropped; nothing else changes."""
    def text(key: str) -> str:
        return str(garment.get(key) or "").strip()

    if set_key == "single":
        return {"GARMENT": text("name")}
    part1, part2 = text("part1_name"), text("part2_name")
    return {
        "DESIGN": text("design_text"),
        "KEEP": text("keep_text"),
        "PART1_LABEL": part1.upper(),
        "PART1": part1.lower(),
        "PART2_LABEL": part2.upper(),
        "PART2": part2.lower(),
    }


def _blank_pattern(set_key: str) -> re.Pattern[str]:
    # Longest first so [PART1_LABEL] is never read as [PART1] (they cannot overlap anyway).
    names = sorted(SET_BLANKS[set_key], key=len, reverse=True)
    return re.compile(r"\[(" + "|".join(re.escape(name) for name in names) + r")\]")


def blanks_used(set_key: str, templates: dict[str, Optional[str]]) -> list[str]:
    pattern = _blank_pattern(set_key)
    found: set[str] = set()
    for template in templates.values():
        found.update(pattern.findall(template or ""))
    return [name for name in SET_BLANKS[set_key] if name in found]


def missing_blanks(set_key: str, templates: dict[str, Optional[str]], values: dict[str, str]) -> list[str]:
    """Form fields still empty although a template uses their blank (no duplicates)."""
    missing: list[str] = []
    for name in blanks_used(set_key, templates):
        source = BLANK_SOURCES[name]
        if not values.get(name) and source not in missing:
            missing.append(source)
    return missing


def fill_blanks(template: Optional[str], set_key: str, values: dict[str, str]) -> str:
    """Replace every occurrence of the set's blanks in ONE pass, so text that was filled in is
    never scanned again. All other characters of the template are kept exactly."""
    if not template:
        return ""
    return _blank_pattern(set_key).sub(lambda match: values[match.group(1)], template)


def render_prompts(set_key: str, version: dict[str, Any], garment: dict[str, Any]) -> dict[str, Optional[str]]:
    """The three garment prompts for a version, or PromptLibraryError when a blank would stay empty."""
    templates = {field: version.get(field) for field in PROMPT_FIELDS}
    values = blank_values(set_key, garment)
    missing = missing_blanks(set_key, templates, values)
    if missing:
        raise PromptLibraryError("Fill in " + ", ".join(missing) + " before saving")
    rendered = {field: fill_blanks(templates[field], set_key, values) for field in PROMPT_FIELDS}
    # Same rule as saving prompts by hand: an empty look try-on prompt is stored as NULL.
    if not (rendered["look_tryon_prompt"] or "").strip():
        rendered["look_tryon_prompt"] = None
    return rendered


# ---------------------------------------------------------------- versions

def next_version(existing_numbers: list[Any]) -> int:
    numbers = [int(n) for n in existing_numbers if n is not None]
    return (max(numbers) if numbers else 0) + 1


def check_version_texts(look_prompt: Optional[str], tryon_prompt: Optional[str]) -> None:
    if not (look_prompt or "").strip() or not (tryon_prompt or "").strip():
        raise PromptLibraryError("Look and Try-on prompts are both required")


def restore_note(version: int, extra: Optional[str] = None) -> str:
    note = f"Restored from Version {version}"
    extra = (extra or "").strip()
    return f"{note} · {extra}" if extra else note


# ---------------------------------------------------------------- slugs and parts

def make_slug(name: Optional[str]) -> str:
    """'Kurta & trousers' -> 'kurta_trousers': lowercase, every run of spaces/symbols -> '_'."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def clean_part_name(value: Optional[str], label: str) -> str:
    name = re.sub(r"\s+", " ", (value or "").strip())
    if not name:
        raise PromptLibraryError(f"{label} name is required")
    if len(name) > PART_NAME_MAX_LENGTH:
        raise PromptLibraryError(f"{label} name must be at most {PART_NAME_MAX_LENGTH} characters")
    if not make_slug(name):
        raise PromptLibraryError(f"{label} name needs at least one letter or number")
    return name


def desired_slots(part1_name: str, part2_name: str) -> list[dict[str, Any]]:
    """The exact two slots a Two fabrics garment must have."""
    slots = [
        {"sort_order": 1, "label": f"{part1_name} fabric", "apply_to": make_slug(part1_name)},
        {"sort_order": 2, "label": f"{part2_name} fabric", "apply_to": make_slug(part2_name)},
    ]
    if slots[0]["apply_to"] == slots[1]["apply_to"]:
        raise PromptLibraryError("Part 1 and Part 2 need different names")
    for slot in slots:
        if slot["apply_to"] in _RESERVED_SLUGS:
            raise PromptLibraryError(f"'{slot['apply_to']}' is reserved for single-slot garments; pick another part name")
    return slots


def plan_slot_changes(
    existing: list[dict[str, Any]], desired: list[dict[str, Any]], *, has_generations: bool
) -> dict[str, list[dict[str, Any]]]:
    """How to turn `existing` slots into `desired` ones.

    Matching is by apply_to first, then by position. A garment that already has generations
    keeps every existing slot and its apply_to (generation_fabrics rows refer to it by
    apply_to): only labels and sort order may change in place. Anything else raises."""
    ordered = sorted(existing, key=lambda s: (int(s.get("sort_order") or 0), str(s.get("created_at") or "")))
    remaining = list(ordered)
    matches: list[tuple[dict[str, Any], Optional[dict[str, Any]]]] = []

    for want in desired:
        hit = next((slot for slot in remaining if str(slot.get("apply_to") or "") == want["apply_to"]), None)
        if hit is not None:
            remaining.remove(hit)
        matches.append((want, hit))

    updates: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    for want, hit in matches:
        if hit is None and remaining:
            hit = remaining.pop(0)
            if has_generations:
                raise PromptLibraryError(
                    f"This garment already has looks made with the '{hit.get('apply_to')}' fabric slot, "
                    "so its parts cannot be renamed. Keep the part names, or create a new garment."
                )
        if hit is None:
            inserts.append(dict(want))
            continue
        changes = {
            key: want[key]
            for key in ("label", "apply_to", "sort_order")
            if str(hit.get(key)) != str(want[key])
        }
        if changes:
            updates.append({"id": hit.get("id"), **changes})

    if remaining and has_generations:
        names = ", ".join(str(slot.get("apply_to")) for slot in remaining)
        raise PromptLibraryError(
            f"This garment already has looks, so its fabric slot(s) {names} cannot be removed. "
            "Create a new garment to change this."
        )
    return {"updates": updates, "inserts": inserts, "deletes": [dict(slot) for slot in remaining]}


# ---------------------------------------------------------------- paste from skill

_KEY_ALIASES = {
    "template": "template",
    "name": "name",
    "garment": "name",
    "garmentname": "name",
    "category": "category",
    "fabrics": "fabrics",
    "fabric": "fabrics",
    "part1": "part1",
    "partone": "part1",
    "part2": "part2",
    "parttwo": "part2",
    "design": "design",
    "keep": "keep",
}
# "(SAM word: pants)" keeps everything up to ")"; without brackets it stops at a comma.
_SAM_IN_BRACKETS_RE = re.compile(r"\(\s*sam\s*words?\s*[:=-]?\s*([^)]*)\)", re.IGNORECASE)
_SAM_RE = re.compile(r"sam\s*words?\s*[:=-]?\s*([^,()\n]*)", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"default\s*[:=-]?\s*(white|black)\b", re.IGNORECASE)
_CATEGORY_WORDS = {"men": "men", "man": "men", "mens": "men", "women": "women", "woman": "women",
                   "womens": "women", "ladies": "women", "unisex": "unisex"}
FILLED_LABELS = {
    "name": "Name",
    "category": "Category",
    "prompt_set": "Fabrics",
    "part1_name": "Part 1",
    "part1_sam_word": "Part 1 SAM word",
    "part2_name": "Part 2",
    "part2_sam_word": "Part 2 SAM word",
    "part2_default": "Part 2 default",
    "design_text": "Design",
    "keep_text": "Keep",
}


def _set_from_words(value: str) -> Optional[str]:
    text = value.lower()
    if re.search(r"\b(two|multi|2)\b", text) or "multi" in text:
        return "two"
    if re.search(r"\b(one|single|1)\b", text):
        return "single"
    return None


def _parse_part(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    sam = _SAM_IN_BRACKETS_RE.search(value) or _SAM_RE.search(value)
    if sam and sam.group(1).strip():
        out["sam_word"] = sam.group(1).strip().strip("\"'").strip()
    default = _DEFAULT_RE.search(value)
    if default:
        out["default"] = default.group(1).lower()
    # The name is whatever comes before the first "(" or ",", minus any SAM / default words.
    name = re.split(r"[(,]", value, maxsplit=1)[0]
    name = _SAM_RE.sub("", name)
    name = _DEFAULT_RE.sub("", name).strip().strip("\"'").strip()
    if name:
        out["name"] = name
    return out


def parse_skill_block(text: Optional[str]) -> dict[str, Any]:
    """Read the block the garment skill prints. Tolerant: any case, extra spaces, markdown
    bullets / bold, "SAM word" or "SAM words", missing lines. Returns
    {"fields": {...only what was found...}, "filled": [labels]}. It never saves anything."""
    values: dict[str, str] = {}
    last_key: Optional[str] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.replace("**", "").replace("__", "").strip()
        line = re.sub(r"^[-*•>#\s]+", "", line).strip()
        if not line:
            last_key = None
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9 ]{0,24}?)\s*[:=]\s*(.*)$", line)
        key = None
        if match:
            key = _KEY_ALIASES.get(re.sub(r"[^a-z0-9]", "", match.group(1).lower()))
        if key:
            values[key] = match.group(2).strip()
            last_key = key
        elif last_key in ("design", "keep"):
            # A wrapped Design / Keep line continues the text.
            values[last_key] = f"{values[last_key]} {line}".strip()

    fields: dict[str, Any] = {}
    if values.get("name"):
        fields["name"] = values["name"]
    category = _CATEGORY_WORDS.get(re.sub(r"[^a-z]", "", values.get("category", "").lower()))
    if category:
        fields["category"] = category

    prompt_set = _set_from_words(values.get("template", "")) or _set_from_words(values.get("fabrics", ""))
    if prompt_set:
        fields["prompt_set"] = prompt_set

    for number in ("1", "2"):
        part = _parse_part(values.get(f"part{number}", ""))
        if part.get("name"):
            fields[f"part{number}_name"] = part["name"]
        if part.get("sam_word"):
            fields[f"part{number}_sam_word"] = part["sam_word"]
        if number == "2" and part.get("default"):
            fields["part2_default"] = part["default"]

    if values.get("design"):
        fields["design_text"] = values["design"]
    if values.get("keep"):
        fields["keep_text"] = values["keep"]

    return {"fields": fields, "filled": [FILLED_LABELS[key] for key in FILLED_LABELS if key in fields]}
