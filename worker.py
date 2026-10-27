import base64
import io
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import httpcore
import httpx
from google import genai
from google.genai import types
from supabase import create_client

import attempt_log
import default_fabrics
from config import get_settings
from image_thumbs import look_thumbnail_path

_MAX_INPUT_IMAGE_DIMENSION = 1536
_THUMB_MAX_WIDTH = 400
_MAX_CONCURRENT_JOBS = 3

_thread_local = threading.local()

_TRANSPORT_ERRORS = (
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpcore.ReadError,
    httpcore.RemoteProtocolError,
    httpcore.ConnectError,
)


def get_supabase():
    """Thread-local Supabase client. Each thread (main loop, each executor
    worker thread) lazily builds and reuses its own client/connection pool
    — sharing one client across threads races inside httpx's sync HTTP/2
    transport and surfaces as intermittent ReadError."""
    client = getattr(_thread_local, "client", None)
    if client is None:
        settings = get_settings()
        client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
        _thread_local.client = client
    return client


def reset_thread_client() -> None:
    _thread_local.client = None


def call_with_retry(fn):
    """Run a zero-arg callable that resolves its own client via
    get_supabase(); on a transport error, drop the thread's client and
    retry once with a fresh one."""
    try:
        return fn()
    except _TRANSPORT_ERRORS as exc:
        print(f"[worker] transport error, resetting client and retrying once: {exc}")
        reset_thread_client()
        time.sleep(1)
        return fn()


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


def build_thumbnail_jpeg_bytes(image_bytes: bytes) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    image.load()
    rgb_image = image.convert("RGB")

    if rgb_image.width > _THUMB_MAX_WIDTH:
        new_height = max(1, round(rgb_image.height * (_THUMB_MAX_WIDTH / rgb_image.width)))
        rgb_image = rgb_image.resize((_THUMB_MAX_WIDTH, new_height), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    rgb_image.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def downscale_image_if_needed(image_bytes: bytes, mime_type: str) -> Tuple[bytes, str]:
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        image.load()
        width, height = image.size

        if max(width, height) <= _MAX_INPUT_IMAGE_DIMENSION:
            return image_bytes, mime_type

        if width >= height:
            new_width = _MAX_INPUT_IMAGE_DIMENSION
            new_height = max(1, round(height * (_MAX_INPUT_IMAGE_DIMENSION / width)))
        else:
            new_height = _MAX_INPUT_IMAGE_DIMENSION
            new_width = max(1, round(width * (_MAX_INPUT_IMAGE_DIMENSION / height)))

        resized = image.convert("RGB").resize((new_width, new_height), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), "image/jpeg"
    except Exception as exc:
        print(f"[worker] WARNING: image downscale failed, using original bytes: {exc}")
        return image_bytes, mime_type


def claim_next_job(supabase) -> Optional[dict]:
    result = supabase.rpc("claim_next_generation_job", {}).execute()
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def claim_next_job_with_retry() -> Optional[dict]:
    try:
        return claim_next_job(get_supabase())
    except _TRANSPORT_ERRORS as exc:
        print(f"[worker] transport error on claim, resetting client: {exc}")
        reset_thread_client()
        time.sleep(1)
        try:
            return claim_next_job(get_supabase())
        except _TRANSPORT_ERRORS as exc2:
            print(f"[worker] transport error on claim retry, giving up this tick: {exc2}")
            return None


def fetch_generation_assets(supabase, job: dict) -> dict:
    generation_id = str(job["id"])
    shop_id = str(job["shop_id"])
    hero_image_id = str(job["hero_image_id"])
    legacy_fabric_image_id = str(job["fabric_image_id"]) if job.get("fabric_image_id") else ""

    def _fetch_generation(columns: str):
        return (
            supabase.table("generations")
            .select(columns)
            .eq("id", generation_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )

    base_columns = "id, shop_id, hero_image_id, fabric_image_id, prompt_used"
    try:
        gen_result = _fetch_generation(f"{base_columns}, default_fabric_slots")
    except Exception as exc:
        # Backward-compatible fallback until the default_fabric_slots column exists.
        if "default_fabric_slots" not in str(exc).lower():
            raise
        gen_result = _fetch_generation(base_columns)
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
        fabric_image_ids = [
            str(row.get("fabric_image_id") or "").strip() for row in mapping_rows
        ]
        if any(not fabric_image_id for fabric_image_id in fabric_image_ids):
            raise RuntimeError("generation_fabrics row missing fabric_image_id")

        fabrics_result = (
            supabase.table("fabric_images")
            .select("id, shop_id, storage_path, mime_type")
            .in_("id", fabric_image_ids)
            .eq("shop_id", shop_id)
            .execute()
        )
        fabric_rows = getattr(fabrics_result, "data", None) or []
        fabrics_by_id = {str(row["id"]): row for row in fabric_rows}

        for row in mapping_rows:
            fabric_image_id = str(row.get("fabric_image_id") or "").strip()
            fabric = fabrics_by_id.get(fabric_image_id)
            if not fabric:
                raise RuntimeError(
                    f"Fabric image metadata missing for fabric_image_id={fabric_image_id}"
                )

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

    # Optional slots left empty use a built-in plain fabric, in that slot's image position.
    default_slots = generation.get("default_fabric_slots")
    if isinstance(default_slots, list) and default_slots:
        mapped_fabrics = default_fabrics.merge_into(mapped_fabrics, default_slots)

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
    ai_info: Optional[dict] = None,
) -> Tuple[bytes, str, Optional[str]]:
    """ai_info (optional) is filled in place for the Operations log: AI times, usage, cost,
    finish reason and, when no image came back, whether the model blocked it."""
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

    if ai_info is not None:
        ai_info.setdefault("ai_started_at", attempt_log.utc_now())
    try:
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
    finally:
        if ai_info is not None:
            ai_info["ai_finished_at"] = attempt_log.utc_now()
    if ai_info is not None:
        ai_info.update(attempt_log.ai_response_fields(response, model_id))

    try:
        image_bytes, output_mime_type = extract_first_image_from_gemini_response(response)
    except RuntimeError:
        if ai_info is not None:
            ai_info["no_image_step"] = attempt_log.no_image_step(response)
        raise

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



def process_job(gemini_client: genai.Client, settings, job: dict) -> None:
    """Process an already-claimed job. Fully self-contained: any failure is
    caught here and routed to mark_generation_failed_with_refund, so this
    can run safely on a worker thread without taking down the pool."""
    generation_id = str(job["id"])
    shop_id = str(job["shop_id"])

    # Operations log: finishes the attempt the API (web or WhatsApp) started. Never raises.
    attempt = attempt_log.WorkerAttempt(generation_id)
    ai_info: dict[str, Any] = {}

    try:
        attempt.step = "upload"  # reading the stored input images
        assets = call_with_retry(lambda: fetch_generation_assets(get_supabase(), job))

        hero = assets["hero"]
        fabrics = assets["fabrics"]
        prompt_used = assets["prompt_used"]

        hero_path = str(hero["storage_path"])
        hero_mime = str(hero.get("mime_type") or "image/jpeg")

        print(f"[worker] downloading hero image: {hero_path}")
        hero_bytes = call_with_retry(
            lambda: download_storage_bytes(get_supabase(), "hero-images", hero_path)
        )
        hero_bytes, hero_mime = downscale_image_if_needed(hero_bytes, hero_mime)

        fabric_inputs: list[dict[str, Any]] = []
        for idx, fabric in enumerate(fabrics, start=1):
            apply_to = str(fabric.get("apply_to") or "unknown")

            if fabric.get("default_fabric"):
                print(
                    "[worker] using built-in default fabric "
                    f"{idx}/{len(fabrics)} apply_to={apply_to}: {fabric['default_fabric']}"
                )
                # A missing file raises here: the job fails and is refunded like any other failure.
                fabric_bytes, fabric_mime = default_fabrics.load(str(fabric["default_fabric"]))
                fabric_bytes, fabric_mime = downscale_image_if_needed(fabric_bytes, fabric_mime)
                fabric_inputs.append({"bytes": fabric_bytes, "mime_type": fabric_mime, "apply_to": apply_to})
                continue

            fabric_path = str(fabric["storage_path"])
            fabric_mime = str(fabric.get("mime_type") or "image/jpeg")

            print(
                "[worker] downloading fabric image "
                f"{idx}/{len(fabrics)} apply_to={apply_to}: {fabric_path}"
            )
            fabric_bytes = call_with_retry(
                lambda fabric_path=fabric_path: download_storage_bytes(
                    get_supabase(), "fabric-images", fabric_path
                )
            )
            fabric_bytes, fabric_mime = downscale_image_if_needed(fabric_bytes, fabric_mime)
            fabric_inputs.append(
                {
                    "bytes": fabric_bytes,
                    "mime_type": fabric_mime,
                    "apply_to": apply_to,
                }
            )

        print(f"[worker] calling Gemini model: {settings.GEMINI_IMAGE_MODEL_ID}")
        attempt.step = "ai_call"
        ai_info["ai_started_at"] = attempt_log.utc_now()
        attempt.update(ai_started_at=ai_info["ai_started_at"], model_id=settings.GEMINI_IMAGE_MODEL_ID)
        output_bytes, output_mime, external_request_id = call_gemini_image_generation(
            gemini_client,
            settings.GEMINI_IMAGE_MODEL_ID,
            prompt_used,
            hero_bytes,
            hero_mime,
            fabric_inputs,
            ai_info=ai_info,
        )

        ext = guess_output_extension(output_mime)
        output_path = f"{shop_id}/{generation_id}/output_v{int(time.time())}.{ext}"

        print(f"[worker] uploading generated output: {output_path}")
        attempt.step = "storage_save"
        call_with_retry(
            lambda: get_supabase().storage.from_("generated-outputs").upload(
                output_path,
                output_bytes,
                {
                    "content-type": output_mime,
                    "upsert": "true",
                },
            )
        )

        thumb_path = look_thumbnail_path(output_path)
        try:
            thumb_bytes = build_thumbnail_jpeg_bytes(output_bytes)
            call_with_retry(
                lambda: get_supabase().storage.from_("generated-outputs").upload(
                    thumb_path,
                    thumb_bytes,
                    {
                        "content-type": "image/jpeg",
                        "upsert": "true",
                    },
                )
            )
            print(f"[worker] uploaded thumbnail: {thumb_path}")
        except Exception as exc:
            print(f"[worker] WARNING: failed to generate/upload thumbnail for {output_path}: {exc}")

        attempt.step = "db_save"
        call_with_retry(
            lambda: mark_generation_done(
                get_supabase(),
                generation_id=generation_id,
                shop_id=shop_id,
                output_path=output_path,
                external_request_id=external_request_id,
            )
        )

        print(f"[worker] generation done: {generation_id}")
        attempt.succeed(output_path=output_path, **_attempt_ai_fields(ai_info))

    except Exception as exc:
        error_message = short_error_message(exc)
        print(f"[worker] generation failed: {generation_id} :: {error_message}")
        traceback.print_exc()

        failed_step = ai_info.get("no_image_step") or attempt.step
        if failed_step == "upload" and "prompt_used is empty" in str(exc):
            failed_step = "prompt_build"  # raised by fetch_generation_assets

        # null = the refund call itself failed, so the log cannot say what was refunded.
        credits_refunded: Optional[int] = None
        try:
            refund_info = mark_generation_failed_with_refund(
                get_supabase(),
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
            credits_refunded = int(refund_info.get("refund_amount") or 0) if refund_info.get("refunded") else 0

        except Exception as mark_exc:
            print(f"[worker] failed to mark generation as failed: {mark_exc}")
            traceback.print_exc()

        attempt.fail(failed_step, error=exc, credits_refunded=credits_refunded, **_attempt_ai_fields(ai_info))


def _attempt_ai_fields(ai_info: dict) -> dict:
    return {key: value for key, value in ai_info.items() if key != "no_image_step"}


_SWEEP_EVERY_SECONDS = 120
_RETENTION_EVERY_SECONDS = 24 * 60 * 60


def run_attempt_log_housekeeping(state: dict) -> None:
    """Operations log upkeep from the main loop: mark stuck attempts timed out (every 2 min)
    and delete log rows older than 90 days (daily; rows only, never images). Never raises."""
    now = time.monotonic()
    try:
        if now - state.get("last_sweep", float("-inf")) >= _SWEEP_EVERY_SECONDS:
            state["last_sweep"] = now
            swept = attempt_log.sweep_timed_out()
            if swept:
                print(f"[worker] attempt log: marked {swept} stuck attempt(s) timed out")
        if now - state.get("last_retention", float("-inf")) >= _RETENTION_EVERY_SECONDS:
            state["last_retention"] = now
            deleted = attempt_log.delete_older_than_retention()
            if deleted:
                print(f"[worker] attempt log: deleted {deleted} row(s) older than 90 days")
    except Exception as exc:
        print(f"[worker] attempt log housekeeping failed: {short_error_message(exc)}")


def main() -> None:
    settings = get_settings()

    if not settings.GEMINI_API_KEY or settings.GEMINI_API_KEY == "PENDING_NANO_KEY":
        raise RuntimeError("Set a real GEMINI_API_KEY in backend/.env before running worker")

    gemini_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    # Worker log writes are synchronous (durable before the next job), on each thread's own client.
    attempt_log.configure(client_factory=get_supabase, sync=True)
    housekeeping_state: dict = {}

    print("[worker] started")
    print(f"[worker] poll interval: {settings.WORKER_POLL_INTERVAL_SECONDS}s")
    print(f"[worker] model: {settings.GEMINI_IMAGE_MODEL_ID}")
    print(f"[worker] max concurrent jobs: {_MAX_CONCURRENT_JOBS}")

    executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_JOBS)
    in_flight: set[Future] = set()

    try:
        while True:
            try:
                claimed_any = False

                while len(in_flight) < _MAX_CONCURRENT_JOBS:
                    job = claim_next_job_with_retry()
                    if not job:
                        break

                    claimed_any = True
                    generation_id = str(job["id"])
                    shop_id = str(job["shop_id"])
                    print(f"[worker] claimed generation={generation_id} shop={shop_id}")

                    future = executor.submit(process_job, gemini_client, settings, job)
                    in_flight.add(future)

                if not claimed_any or len(in_flight) >= _MAX_CONCURRENT_JOBS:
                    time.sleep(settings.WORKER_POLL_INTERVAL_SECONDS)

                completed = {future for future in in_flight if future.done()}
                for future in completed:
                    in_flight.discard(future)
                    try:
                        future.result()
                    except Exception as exc:
                        print(f"[worker] job future raised: {short_error_message(exc)}")
                        traceback.print_exc()

                run_attempt_log_housekeeping(housekeeping_state)

            except Exception as exc:
                print(f"[worker] loop error: {short_error_message(exc)}")
                traceback.print_exc()
                # Prevent transient network/service errors from crashing the worker process.
                time.sleep(max(5, settings.WORKER_POLL_INTERVAL_SECONDS))
    finally:
        executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
