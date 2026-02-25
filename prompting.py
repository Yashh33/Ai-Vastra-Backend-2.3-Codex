def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


BASE_GARMENT_FABRIC_PROMPT = """
Task: Using the provided hero image and fabric image, generate a visualization of how the garment in the hero image would look if made from the provided fabric.

Hard constraints:
- Preserve the same person/model, face, body shape, pose, expression, camera angle, framing, composition, background, surroundings, and overall scene.
- Preserve the same garment type and silhouette from the hero image.
- Change only the garment material appearance so it matches the provided fabric image (texture, pattern, print, weave, color behavior).
- Keep lighting and shadows consistent with the original hero image.
- Do not alter non-garment areas.
- Do not add or remove accessories, objects, or people.
- Avoid artifacts, duplicated body parts, warped garment edges, or unrealistic folds.

Output requirements:
- Return one photorealistic image.
- Maintain visual realism and stitching/fabric drape consistency.
""".strip()


def build_generation_prompt(
    folder_name: str | None,
    folder_prompt_template: str | None,
) -> str:
    folder_name_clean = _clean_text(folder_name)
    folder_prompt_clean = _clean_text(folder_prompt_template)

    parts = [BASE_GARMENT_FABRIC_PROMPT]

    if folder_name_clean:
        parts.append(f"Garment category context: {folder_name_clean}")

    if folder_prompt_clean:
        parts.append("Category-specific instructions:")
        parts.append(folder_prompt_clean)

    return "\n\n".join(parts).strip()
