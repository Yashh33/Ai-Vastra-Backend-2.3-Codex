def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


BASE_GARMENT_FABRIC_PROMPT = """
Hard constraints:
- Preserve the same person/model, face, body shape, pose, expression, camera angle, framing, composition, background, surroundings, and overall scene.
- Preserve garment type, silhouette, seams, folds, and construction details from the hero image.
- Treat each fabric image as a material reference (texture, weave, print, color behavior), not a pasted overlay.
- Maintain realistic drape, stitching behavior, lighting, and shadows.
- Do not alter non-garment areas.
- Do not add or remove accessories, objects, or people.
- Avoid artifacts, duplicated body parts, warped garment edges, or unrealistic folds.

Output requirements:
- Return exactly one photorealistic image.
- Maintain visual realism and garment construction consistency.
- Preserve the original hero image aspect ratio.
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

Generate a photorealistic visualization of how the selected garment in the hero image would look if tailored from Fabric image 1.
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

Generate one photorealistic composite visualization where each garment target appears naturally tailored from its assigned fabric image.
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
