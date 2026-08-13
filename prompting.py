def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


FABRIC_SCALE_HINTS = {
    "fine": (
        "Fabric scale reference: The pattern in the tiled fabric "
        "image is shown at the density it should appear on the "
        "finished garment. Match this pattern density precisely — "
        "checks approximately 4-6mm wide, roughly 15-20 horizontal "
        "repetitions visible across the jacket front."
    ),
    "medium": (
        "Fabric scale reference: The pattern in the tiled fabric "
        "image is shown at the density it should appear on the "
        "finished garment. Match this pattern density precisely — "
        "checks approximately 10-15mm wide, roughly 8-10 horizontal "
        "repetitions visible across the jacket front."
    ),
    "bold": (
        "Fabric scale reference: The pattern in the tiled fabric "
        "image is shown at the density it should appear on the "
        "finished garment. Match this pattern density precisely — "
        "checks approximately 25-35mm wide, roughly 4-5 horizontal "
        "repetitions visible across the jacket front."
    ),
}


def _format_apply_to_label(value: str) -> str:
    mapping = {
        "shirt": "SHIRT",
        "pant": "PANT",
        "suit_full_body": "SUIT FULL BODY",
        "suit_upper": "SUIT UPPER",
        "koti": "KOTI",
    }
    return mapping.get(value, value.upper())


def _build_task_intro(
    fabric_assignments: list[dict[str, str]],
    folder_name: str | None = None,
    fabric_scale: str | None = None,
) -> str:
    if len(fabric_assignments) <= 1:
        scale_line = (
            f"\n{FABRIC_SCALE_HINTS[fabric_scale]}"
            if fabric_scale and fabric_scale in FABRIC_SCALE_HINTS
            else ""
        )
        return f"""Image 1: hero photograph — the EXACT garment to \
reproduce (garment type, cut, length, collar, sleeves, closures, \
construction) plus pose, identity, scene, and lighting.
Image 2: fabric material reference — use ONLY for color, weave, \
sheen, and texture.{scale_line}

Generate a new photograph of the same person, in the same pose \
and scene, wearing the SAME garment shown in Image 1 — identical \
garment type and construction — re-tailored from the cloth in \
Image 2. Do not change the garment type or invent a different \
garment (for example, do not turn it into a suit).""".strip()

    mapping_lines = [
        f"- Fabric image {idx} -> "
        f"{_format_apply_to_label(a.get('apply_to', 'unknown'))}"
        for idx, a in enumerate(fabric_assignments, start=1)
    ]
    scale_line = (
        f"\n{FABRIC_SCALE_HINTS[fabric_scale]}"
        if fabric_scale and fabric_scale in FABRIC_SCALE_HINTS
        else ""
    )
    return f"""Image 1: hero photograph — use for pose, identity, scene, \
lighting, and garment cut.
Images 2..N: fabric material references — each assigned to a \
garment target below.{scale_line}

The garment type, cut, and construction must match Image 1 (the \
hero) exactly; apply each fabric only to its assigned region.

Generate a new photograph of the same person, in the same pose \
and scene, wearing garments tailored from their assigned cloth.
Fabric-to-garment mapping:
{chr(10).join(mapping_lines)}""".strip()


BASE_GARMENT_FABRIC_PROMPT = """
The output garment MUST be the same garment shown in the hero \
image — same garment type, silhouette, length, collar, sleeves, \
closures, and construction. Do NOT substitute, restyle, or \
invent a different garment; only the fabric/material changes.

The cloth from the fabric image has been cut and sewn into \
the garment by a skilled tailor. Reconstruct how this cloth \
would look as a finished, worn garment:

- Study the fabric's color, weave structure, pattern repeat, \
sheen, and texture depth from the reference image.
- Reproduce how this material drapes, folds, and catches \
studio light when worn — including natural creasing at the \
elbows, chest, and waist.
- Pattern lines follow the garment's seams and contours; \
they are not flat or perfectly grid-aligned across the \
whole surface.
- Person, face, body, pose, expression, camera angle, \
framing, background, and scene remain exactly as in \
the hero image.

Output: one photorealistic studio fashion photograph.
""".strip()


def build_generation_prompt(
    folder_name: str | None,
    folder_prompt_template: str | None,
    fabric_assignments: list[dict[str, str]] | None = None,
    fabric_scale: str | None = None,
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
        _build_task_intro(
            normalized_assignments,
            folder_name=folder_name_clean,
            fabric_scale=fabric_scale,
        ),
        BASE_GARMENT_FABRIC_PROMPT,
    ]

    if folder_prompt_clean:
        parts.append("Category-specific instructions:")
        parts.append(folder_prompt_clean)

    return "\n\n".join(parts).strip()


def build_tryon_prompt(folder_name: str | None = None) -> str:
    folder_str = f" {folder_name.strip()}" if folder_name and folder_name.strip() else ""

    return f"""Image 1: Customer photograph — use for the \
person's identity, face, body shape, body proportions, \
skin tone, and pose.
Image 2: Garment visualization — a professionally \
tailored{folder_str} garment in a specific fabric. Use \
for the complete garment appearance including fabric \
color, texture, pattern, lapel style, buttons, collar, \
and all construction details.

Generate a new photograph of the person from Image 1 \
wearing the exact garment shown in Image 2.

The garment must appear exactly as shown in Image 2 — \
preserve the fabric pattern, color, texture, lapel shape, \
button placement, and all construction details precisely. \
The person's face, body proportions, and skin tone from \
Image 1 must be preserved exactly.

The result must look like a natural photograph of the \
customer wearing a garment tailored specifically for them.

Do not change or replace the background — use the exact background from Image 1 (the customer photograph).
Do not beautify, retouch, or alter the person's appearance — keep their face, skin tone, features, and expression exactly as they appear in Image 1.

Output: one photorealistic photograph.""".strip()


def build_tryon_quick_prompt(folder_name: str | None = None) -> str:
    return f"""Image 1: Hero garment photograph — use for \
garment cut, silhouette, lapel style, button placement, \
collar, seams, and all construction details.
Image 2: Fabric swatch — use for color, weave structure, \
pattern, sheen, and texture.
Image 3: Customer photograph — use for the person's \
identity, face, body shape, body proportions, skin tone, \
and pose.

Generate a new photograph of the customer from Image 3 \
wearing the SAME garment type shown in Image 1 (same cut, \
silhouette, collar, sleeves, closures, construction), \
tailored from the cloth in Image 2. Do not change the \
garment type.

The garment construction details (lapels, buttons, collar, \
seams) must match Image 1 exactly. The fabric material \
(color, pattern, texture) must come from Image 2. The \
person (face, body, proportions, skin tone) must come \
from Image 3.

The result must look like a natural photograph of the \
customer wearing a garment tailored specifically for them.

Do not change or replace the background — use the exact background from Image 1 (the customer photograph).
Do not beautify, retouch, or alter the person's appearance — keep their face, skin tone, features, and expression exactly as they appear in Image 1.

Output: one photorealistic photograph.""".strip()


def build_tryon_multi_quick_prompt(fabric_assignments, folder_name):
    lines = []
    lines.append("Image 1: Hero garment photograph — use for garment cut, silhouette, lapel style, button placement, collar, seams, and all construction details.")

    for i, assignment in enumerate(fabric_assignments):
        img_num = i + 2
        lines.append(f"Image {img_num}: Fabric swatch — use for the {assignment['apply_to'].replace('_', ' ')} portion of the garment. Color, weave structure, pattern, sheen, and texture.")

    customer_img_num = len(fabric_assignments) + 2
    lines.append(f"Image {customer_img_num}: Customer photograph — use for the person's identity, face, body shape, body proportions, skin tone, and pose.")
    lines.append("")
    lines.append(f"Generate a new photograph of the customer from Image {customer_img_num} wearing a {folder_name} garment with the cut and construction of Image 1, tailored from the fabrics shown:")

    for i, assignment in enumerate(fabric_assignments):
        img_num = i + 2
        lines.append(f"- Image {img_num} fabric → {assignment['apply_to'].replace('_', ' ')}")

    lines.append("")
    lines.append("The garment construction details (lapels, buttons, collar, seams) must match Image 1 exactly. Each fabric must be applied only to its designated garment part. The person (face, body, proportions, skin tone) must come from the customer photograph.")
    lines.append("")
    lines.append("The result must look like a natural photograph of the customer wearing a garment tailored specifically for them.")
    lines.append("")
    lines.append("Do not change or replace the background — use the exact background from the customer photograph.")
    lines.append("Do not beautify, retouch, or alter the person's appearance — keep their face, skin tone, features, and expression exactly as they appear in the customer photograph.")
    lines.append("")
    lines.append("Output: one photorealistic photograph.")

    return "\n".join(lines)
