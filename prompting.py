def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def _format_apply_to_label(value: str) -> str:
    mapping = {
        "shirt": "SHIRT",
        "pant": "PANT",
        "suit_full_body": "SUIT FULL BODY",
        "suit_upper": "SUIT UPPER",
        "koti": "KOTI",
    }
    return mapping.get(value, value.upper())


DEFAULT_LOOK_PROMPT = (
    "Image 1: hero photograph — the EXACT garment to reproduce (garment "
    "type, cut, length, collar, sleeves, closures, construction) plus pose, "
    "identity, scene, and lighting. Remaining images: fabric material "
    "references — use ONLY for color, weave, sheen, and texture.\n\n"
    "Generate a new photograph of the same person, in the same pose and "
    "scene, wearing the SAME garment shown in Image 1 for {garment_name} — "
    "identical garment type and construction — re-tailored from the "
    "cloth shown in the fabric image(s). Do not change the garment type or "
    "invent a different garment.\n"
    "{fabric_mapping}\n\n"
    "Output: one photorealistic studio fashion photograph."
)

DEFAULT_TRYON_PROMPT = (
    "Generate a photorealistic photograph of the customer wearing the "
    "{garment_name} garment shown in the reference image(s). Preserve the "
    "customer's face, body proportions, skin tone, and pose exactly. "
    "Preserve the garment's construction, fabric color, texture, and "
    "pattern exactly as shown. Do not change or replace the background — "
    "keep the customer's original background. Do not beautify, retouch, "
    "or alter the person's appearance.\n"
    "{fabric_mapping}\n\n"
    "Output: one photorealistic photograph."
)


def build_fabric_mapping(fabric_assignments: list[dict[str, str]] | None) -> str:
    """Return "Fabric N -> PART" lines for a multi-fabric garment, or an
    empty string when there is only one fabric (or none)."""
    assignments = fabric_assignments or []
    if len(assignments) <= 1:
        return ""

    lines = [
        f"Fabric {idx} -> {_format_apply_to_label(a.get('apply_to', 'unknown'))}"
        for idx, a in enumerate(assignments, start=1)
    ]
    return "\n".join(lines)


def fill_prompt_placeholders(
    template: str,
    *,
    garment_name: str | None = None,
    fabric_assignments: list[dict[str, str]] | None = None,
    image_count: int | None = None,
) -> str:
    """Fill the dynamic placeholders a stored garment prompt may contain:
    {garment_name}, {fabric_mapping}, {image_count}. A template with none of
    these tokens is returned verbatim. Unrelated braces in admin-authored
    text are left untouched — only these exact tokens are replaced."""
    result = template

    if "{garment_name}" in result:
        result = result.replace("{garment_name}", _clean_text(garment_name) or "the garment")

    if "{fabric_mapping}" in result:
        result = result.replace("{fabric_mapping}", build_fabric_mapping(fabric_assignments))

    if "{image_count}" in result:
        result = result.replace(
            "{image_count}", str(image_count) if image_count is not None else ""
        )

    return result.strip()
