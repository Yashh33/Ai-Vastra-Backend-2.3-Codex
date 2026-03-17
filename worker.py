import base64
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from google import genai
from google.genai import types

from config import get_settings
from supabase_client import get_supabase_admin_client


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def short_error_message(exc: Exception, limit: int = 1500) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:limit]


def guess_output_extension(mime_type: str) -> str:
    mime = (mime_type or "").lower()
    if "png" in mime:
        return "png"
    if "jpeg" in mime or "jpg" in mime:
        return "jpg"
    if "webp" in mime:
        return "webp"
    return "png"


def claim_next_job(supabase) -> Optional[dict]:
    result = supabase.rpc("claim_next_generation_job", {}).execute()
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def fetch_generation_assets(supabase, job: dict) -> dict:
    generation_id = str(job["id"])
    shop_id = str(job["shop_id"])
    hero_image_id = str(job["hero_image_id"])
    legacy_fabric_image_id = str(job["fabric_image_id"]) if job.get("fabric_image_id") else ""

    gen_result = (
        supabase.table("generations")
        .select("id, shop_id, hero_image_id, fabric_image_id, prompt_used")
        .eq("id", generation_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    gen_rows = getattr(gen_result, "data", None) or []
    if not gen_rows:
        raise RuntimeError("Generation row not found after claim")
    generation = gen_rows[0]

    hero_result = (
        supabase.table("hero_images")
        .select("id, shop_id, folder_id, storage_path, mime_type")
        .eq("id", hero_image_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    hero_rows = getattr(hero_result, "data", None) or []
    if not hero_rows:
        raise RuntimeError("Hero image metadata missing")
    hero = hero_rows[0]

    mapping_result = (
        supabase.table("generation_fabrics")
        .select("fabric_image_id, apply_to, sort_order")
        .eq("generation_id", generation_id)
        .eq("shop_id", shop_id)
        .order("sort_order", desc=False)
        .execute()
    )
    mapping_rows = getattr(mapping_result, "data", None) or []

    mapped_fabrics: list[dict] = []

    if mapping_rows:
        for row in mapping_rows:
            fabric_image_id = str(row.get("fabric_image_id") or "").strip()
            if not fabric_image_id:
                raise RuntimeError("generation_fabrics row missing fabric_image_id")

            fabric_result = (
                supabase.table("fabric_images")
                .select("id, shop_id, storage_path, mime_type")
                .eq("id", fabric_image_id)
                .eq("shop_id", shop_id)
                .limit(1)
                .execute()
            )
            fabric_rows = getattr(fabric_result, "data", None) or []
            if not fabric_rows:
                raise RuntimeError(
                    f"Fabric image metadata missing for fabric_image_id={fabric_image_id}"
                )

            fabric = fabric_rows[0]
            mapped_fabrics.append(
                {
                    "id": str(fabric["id"]),
                    "storage_path": str(fabric["storage_path"]),
                    "mime_type": str(fabric.get("mime_type") or "image/jpeg"),
                    "apply_to": str(row.get("apply_to") or ""),
                    "sort_order": int(row.get("sort_order") or 0),
                }
            )
    else:
        # Backward-compatible fallback for legacy single-fabric generations.
        if not legacy_fabric_image_id:
            raise RuntimeError("Fabric image metadata missing")

        fabric_result = (
            supabase.table("fabric_images")
            .select("id, shop_id, storage_path, mime_type")
            .eq("id", legacy_fabric_image_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
        fabric_rows = getattr(fabric_result, "data", None) or []
        if not fabric_rows:
            raise RuntimeError("Fabric image metadata missing")

        fabric = fabric_rows[0]
        mapped_fabrics.append(
            {
                "id": str(fabric["id"]),
                "storage_path": str(fabric["storage_path"]),
                "mime_type": str(fabric.get("mime_type") or "image/jpeg"),
                "apply_to": "suit_full_body",
                "sort_order": 1,
            }
        )

    prompt_used = generation.get("prompt_used")
    if not prompt_used:
        raise RuntimeError("prompt_used is empty; create generation again after Step 21")

    return {
        "generation": generation,
        "hero": hero,
        "fabrics": mapped_fabrics,
        "prompt_used": str(prompt_used),
    }


def download_storage_bytes(supabase, bucket: str, path: str) -> bytes:
    if not path or not path.strip():
        raise RuntimeError(f"Invalid storage path for bucket={bucket}")
    return supabase.storage.from_(bucket).download(path.strip())


def _iter_response_parts(response: Any):
    parts = getattr(response, "parts", None)
    if parts:
        for part in parts:
            yield part

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        candidate_parts = getattr(content, "parts", None) or []
        for part in candidate_parts:
            yield part


def extract_first_image_from_gemini_response(response: Any) -> Tuple[bytes, str]:
    for part in _iter_response_parts(response):
        inline_data = getattr(part, "inline_data", None)
        if not inline_data:
            continue

        data = getattr(inline_data, "data", None)
        mime_type = getattr(inline_data, "mime_type", None) or "image/png"

        if not data:
            continue

        if isinstance(data, bytes):
            return data, mime_type

        if isinstance(data, str):
            # Defensive: SDKs sometimes return base64 text
            return base64.b64decode(data), mime_type

    raise RuntimeError("No image output found in Gemini response")


def call_gemini_image_generation(
    client: genai.Client,
    model_id: str,
    prompt: str,
    hero_image_bytes: bytes,
    hero_mime_type: str,
    fabric_inputs: list[dict[str, Any]],
) -> Tuple[bytes, str, Optional[str]]:
    contents: list[Any] = [
        prompt,
        types.Part.from_bytes(
            data=hero_image_bytes,
            mime_type=hero_mime_type or "image/jpeg",
        ),
    ]

    for fabric_input in fabric_inputs:
        contents.append(
            types.Part.from_bytes(
                data=fabric_input["bytes"],
                mime_type=str(fabric_input.get("mime_type") or "image/jpeg"),
            )
        )

    response = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
            aspect_ratio="3:4",
            image_size="2K",
            ),
        ),
    )

    image_bytes, output_mime_type = extract_first_image_from_gemini_response(response)

    # Keep existing DB column name `nano_request_id` for now (schema already created).
    external_request_id = (
        getattr(response, "response_id", None)
        or getattr(response, "id", None)
        or None
    )

    return image_bytes, output_mime_type, external_request_id


def mark_generation_done(
    supabase,
    *,
    generation_id: str,
    shop_id: str,
    output_path: str,
    external_request_id: Optional[str],
) -> None:
    payload = {
        "status": "done",
        "output_path": output_path,
        "completed_at": utc_now_iso(),
        "error": None,
    }
    if external_request_id:
        payload["nano_request_id"] = external_request_id

    (
        supabase.table("generations")
        .update(payload)
        .eq("id", generation_id)
        .eq("shop_id", shop_id)
        .eq("status", "processing")
        .execute()
    )


# def mark_generation_failed(
#     supabase,
#     *,
#     generation_id: str,
#     shop_id: str,
#     error_message: str,
# ) -> None:
#     (
#         supabase.table("generations")
#         .update(
#             {
#                 "status": "failed",
#                 "error": error_message,
#                 "completed_at": utc_now_iso(),
#             }
#         )
#         .eq("id", generation_id)
#         .eq("shop_id", shop_id)
#         .eq("status", "processing")
#         .execute()
#     )

def mark_generation_failed_with_refund(
    supabase,
    *,
    generation_id: str,
    shop_id: str,
    error_message: str,
) -> dict:
    result = (
        supabase.rpc(
            "mark_generation_failed_with_refund",
            {
                "p_generation_id": generation_id,
                "p_shop_id": shop_id,
                "p_error": error_message,
            },
        )
        .execute()
    )

    rows = getattr(result, "data", None) or []
    if not rows:
        raise RuntimeError("Failed to mark generation as failed with refund")

    return rows[0]



def process_one_job(supabase, gemini_client: genai.Client, settings) -> bool:
    job = claim_next_job(supabase)
    if not job:
        return False

    generation_id = str(job["id"])
    shop_id = str(job["shop_id"])
    print(f"[worker] claimed generation={generation_id} shop={shop_id}")

    try:
        assets = fetch_generation_assets(supabase, job)

        hero = assets["hero"]
        fabrics = assets["fabrics"]
        prompt_used = assets["prompt_used"]

        hero_path = str(hero["storage_path"])
        hero_mime = str(hero.get("mime_type") or "image/jpeg")

        print(f"[worker] downloading hero image: {hero_path}")
        hero_bytes = download_storage_bytes(supabase, "hero-images", hero_path)

        fabric_inputs: list[dict[str, Any]] = []
        for idx, fabric in enumerate(fabrics, start=1):
            fabric_path = str(fabric["storage_path"])
            fabric_mime = str(fabric.get("mime_type") or "image/jpeg")
            apply_to = str(fabric.get("apply_to") or "unknown")

            print(
                "[worker] downloading fabric image "
                f"{idx}/{len(fabrics)} apply_to={apply_to}: {fabric_path}"
            )
            fabric_bytes = download_storage_bytes(supabase, "fabric-images", fabric_path)
            fabric_inputs.append(
                {
                    "bytes": fabric_bytes,
                    "mime_type": fabric_mime,
                    "apply_to": apply_to,
                }
            )

        print(f"[worker] calling Gemini model: {settings.GEMINI_IMAGE_MODEL_ID}")
        output_bytes, output_mime, external_request_id = call_gemini_image_generation(
            gemini_client,
            settings.GEMINI_IMAGE_MODEL_ID,
            prompt_used,
            hero_bytes,
            hero_mime,
            fabric_inputs,
        )

        ext = guess_output_extension(output_mime)
        output_path = f"{shop_id}/{generation_id}/output.{ext}"

        print(f"[worker] uploading generated output: {output_path}")
        supabase.storage.from_("generated-outputs").upload(
            output_path,
            output_bytes,
            {
                "content-type": output_mime,
                "upsert": "false",
            },
        )

        mark_generation_done(
            supabase,
            generation_id=generation_id,
            shop_id=shop_id,
            output_path=output_path,
            external_request_id=external_request_id,
        )

        print(f"[worker] generation done: {generation_id}")
        return True

    except Exception as exc:
        error_message = short_error_message(exc)
        print(f"[worker] generation failed: {generation_id} :: {error_message}")
        traceback.print_exc()

        # try:
        #     mark_generation_failed(
        #         supabase,
        #         generation_id=generation_id,
        #         shop_id=shop_id,
        #         error_message=error_message,
        #     )
        try:
            refund_info = mark_generation_failed_with_refund(
                supabase,
                generation_id=generation_id,
                shop_id=shop_id,
                error_message=error_message,
            )
            print(
                "[worker] marked failed "
                f"generation={generation_id} refunded={refund_info.get('refunded')} "
                f"refund_amount={refund_info.get('refund_amount')} "
                f"balance_after={refund_info.get('balance_after')}"
            )

        except Exception as mark_exc:
            print(f"[worker] failed to mark generation as failed: {mark_exc}")
            traceback.print_exc()

        return True  # A job was claimed/handled, even if failed.


def main() -> None:
    settings = get_settings()
    supabase = get_supabase_admin_client()

    if not settings.GEMINI_API_KEY or settings.GEMINI_API_KEY == "PENDING_NANO_KEY":
        raise RuntimeError("Set a real GEMINI_API_KEY in backend/.env before running worker")

    gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)

    print("[worker] started")
    print(f"[worker] poll interval: {settings.WORKER_POLL_INTERVAL_SECONDS}s")
    print(f"[worker] model: {settings.GEMINI_IMAGE_MODEL_ID}")

    while True:
        try:
            handled = process_one_job(supabase, gemini_client, settings)
            if not handled:
                time.sleep(settings.WORKER_POLL_INTERVAL_SECONDS)
        except Exception as exc:
            print(f"[worker] loop error: {short_error_message(exc)}")
            traceback.print_exc()
            # Prevent transient network/service errors from crashing the worker process.
            time.sleep(max(5, settings.WORKER_POLL_INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
