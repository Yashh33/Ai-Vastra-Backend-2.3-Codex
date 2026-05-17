def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


BASE_GARMENT_FABRIC_PROMPT = """
Realism rules:
- Imagine this garment was originally sewn and tailored from
  the exact cloth shown in the fabric swatch image. Your output
  should look like a real fashion photograph of a person wearing
  a garment made from that cloth — not a texture overlay or
  digital edit.
- Use the fabric swatch as a material reference: study its color,
  weave structure, pattern (checks, stripes, plain, etc.), sheen,
  and texture depth. Reconstruct how this material would drape,
  fold, and catch light when sewn into a fitted garment.
- Pattern scale is critical: fine patterns like checks,
  herringbone, stripes, and small prints must appear at real
  garment scale — small and tight as seen on actual tailored
  cloth at normal viewing distance. Never enlarge or zoom the
  pattern. A check that appears 1cm on the swatch should appear
  proportionally tiny on the garment.
- The fabric wraps around the body naturally: pattern lines
  follow the garment's seams, folds, and contours. Patterns
  do not stay flat or perfectly grid-aligned across the whole
  garment.
- Preserve from the hero image without any change: the person,
  face, body, pose, expression, camera angle, framing,
  composition, background, scene, garment silhouette, cut,
  lapels, buttons, collar, seams, and all construction details.
- Only the cloth material changes. Every other pixel stays
  the same.
- Do not alter anything outside the target garment area.
- No texture pasting, no overlay effects, no Photoshop-style
  blending. Pure photorealistic synthesis.
- No artifacts, warped edges, duplicate body parts, or
  unrealistic folds.

Output: one photorealistic studio fashion photograph.
""".strip()


def _format_apply_to_label(value: str) -> str:
    mapping = {
        "shirt": "SHIRT",
        "pant": "PANT",
        "suit_full_body": "SUIT FULL BODY",
        "suit_upper": "SUIT UPPER",
        "koti": "KOTI",
    }
    return mapping.get(value, value.upper())


def _build_task_intro(fabric_assignments: list[dict[str, str]]) -> str:
    if len(fabric_assignments) <= 1:
        target = _format_apply_to_label(
            (fabric_assignments[0].get("apply_to") if fabric_assignments else "suit_full_body")
        )
        return f"""
Task:
You are given 2 images in this exact order:
1) Hero image
2) Fabric image 1

Recreate the hero image as a photorealistic fashion photograph where the {target} garment has been sewn and tailored from the cloth material shown in Fabric image 1. The result must look like the person was always wearing a garment made from that specific cloth — not a digital edit.
Selected garment target: {target}.
""".strip()

    mapping_lines: list[str] = []
    for idx, assignment in enumerate(fabric_assignments, start=1):
        mapping_lines.append(
            f"- Fabric image {idx} -> {_format_apply_to_label(assignment.get('apply_to', 'unknown'))}"
        )

    return f"""
Task:
You are given multiple images in this exact order:
1) Hero image
2..N) Fabric images in the same order listed below

Recreate the hero image as a photorealistic fashion photograph where each garment has been sewn and tailored from its assigned cloth material. Each result must look like the person was always wearing garments made from those specific cloths — not a digital edit.
Fabric-to-garment mapping:
{chr(10).join(mapping_lines)}

Do not swap, mix, or blend assignments across garment targets.
""".strip()


def build_generation_prompt(
    folder_name: str | None,
    folder_prompt_template: str | None,
    fabric_assignments: list[dict[str, str]] | None = None,
) -> str:
    folder_name_clean = _clean_text(folder_name)
    folder_prompt_clean = _clean_text(folder_prompt_template)

    normalized_assignments: list[dict[str, str]] = []
    for item in fabric_assignments or []:
        apply_to = _clean_text(item.get("apply_to"))
        if not apply_to:
            continue
        normalized_assignments.append({"apply_to": apply_to})

    if not normalized_assignments:
        normalized_assignments = [{"apply_to": "suit_full_body"}]

    parts = [
        _build_task_intro(normalized_assignments),
        BASE_GARMENT_FABRIC_PROMPT,
    ]

    if folder_name_clean:
        parts.append(f"Garment category context: {folder_name_clean}")

    if folder_prompt_clean:
        parts.append("Category-specific instructions:")
        parts.append(folder_prompt_clean)

    return "\n\n".join(parts).strip()
