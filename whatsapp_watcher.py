import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional

import anyio.to_thread

from config import get_settings
from supabase_client import get_supabase_admin_client
from whatsapp_transport import send_image_by_link, send_text

_POLL_INTERVAL_SECONDS = 15
_STUCK_PROCESSING_MINUTES = 15

_MSG_LOOK_READY_CAPTION = (
    "Aapka look ready hai! 🎉✨\nNaya look banana ho to apne agle FABRIC ki "
    "photo bhejiye 📸"
    "\n\n👤 Is look ko apne CUSTOMER par dekhna hai? Reply TRYON"
)
_MSG_LAST_FREE_LOOK = "Ye aapka aakhri free look tha 🙏 Paid plans jald aa rahe hain!"
_MSG_GENERATION_FAILED = (
    "Look banane mein problem aa gayi 😔 Aapka credit wapas ho gaya hai. "
    "'reset' bhej ke dobara try kijiye."
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_processing_sessions_sync(supabase) -> list[dict]:
    result = (
        supabase.table("whatsapp_sessions")
        .select("*")
        .eq("state", "PROCESSING")
        .execute()
    )
    return getattr(result, "data", None) or []


def _fetch_generation_sync(supabase, generation_id: str, shop_id: str) -> Optional[dict]:
    result = (
        supabase.table("generations")
        .select("id, status, output_path")
        .eq("id", generation_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def _get_shop_balance_sync(supabase, shop_id: str) -> int:
    result = (
        supabase.table("credit_ledger")
        .select("balance_after")
        .eq("shop_id", shop_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        return 0
    return int(rows[0].get("balance_after") or 0)


def _extract_signed_url(payload: object) -> Optional[str]:
    if isinstance(payload, dict):
        nested = payload.get("data")
        nested = nested if isinstance(nested, dict) else {}
        return (
            payload.get("signedURL")
            or payload.get("signedUrl")
            or payload.get("signed_url")
            or nested.get("signedURL")
            or nested.get("signedUrl")
            or nested.get("signed_url")
        )
    return None


def _create_signed_url_sync(supabase, output_path: str, expires_in: int) -> Optional[str]:
    try:
        signed = (
            supabase.storage.from_("generated-outputs")
            .create_signed_url(output_path, expires_in)
        )
    except Exception as exc:
        print(f"[whatsapp_watcher] failed to sign url for {output_path}: {exc}")
        return None

    signed_url = _extract_signed_url(signed)
    if not signed_url:
        return None

    if signed_url.startswith("/"):
        signed_url = f"{get_settings().SUPABASE_URL}{signed_url}"

    return signed_url


def _update_session_sync(supabase, session_id: str, payload: dict) -> None:
    payload = dict(payload)
    payload["last_message_at"] = _utc_now_iso()
    supabase.table("whatsapp_sessions").update(payload).eq("id", session_id).execute()


def _is_stuck(session: dict) -> bool:
    raw_ts = session.get("last_message_at") or session.get("updated_at")
    if not raw_ts:
        return False

    try:
        ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except ValueError:
        return False

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    return age_seconds > _STUCK_PROCESSING_MINUTES * 60


def _process_session_sync(supabase, session: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]
    generation_id = session.get("active_generation_id")

    if not generation_id:
        if _is_stuck(session):
            send_text(phone, _MSG_GENERATION_FAILED)
            _update_session_sync(
                supabase,
                session["id"],
                {"state": "ERROR", "error_detail": "PROCESSING session missing active_generation_id"},
            )
        return

    generation = _fetch_generation_sync(supabase, generation_id, shop_id)
    if not generation:
        if _is_stuck(session):
            send_text(phone, _MSG_GENERATION_FAILED)
            _update_session_sync(
                supabase,
                session["id"],
                {"state": "ERROR", "error_detail": "Generation row not found"},
            )
        return

    gen_status = str(generation.get("status") or "")

    if gen_status == "done":
        output_path = str(generation.get("output_path") or "").strip()
        if not output_path:
            send_text(phone, _MSG_GENERATION_FAILED)
            _update_session_sync(
                supabase,
                session["id"],
                {"state": "ERROR", "error_detail": "Generation done but output_path missing"},
            )
            return

        signed_url = _create_signed_url_sync(supabase, output_path, 3600)
        if not signed_url:
            # Transient signing failure — leave state as-is and retry next tick.
            return

        send_image_by_link(phone, signed_url, caption=_MSG_LOOK_READY_CAPTION)

        free_generations_used = int(session.get("free_generations_used") or 0) + 1
        _update_session_sync(
            supabase,
            session["id"],
            {
                "state": "OFFER_TRYON",
                "free_generations_used": free_generations_used,
            },
        )

        balance = _get_shop_balance_sync(supabase, shop_id)
        if balance < get_settings().CREDITS_PER_IMAGE:
            send_text(phone, _MSG_LAST_FREE_LOOK)
        return

    if gen_status == "failed":
        send_text(phone, _MSG_GENERATION_FAILED)
        _update_session_sync(supabase, session["id"], {"state": "ERROR"})
        return

    # queued / processing: still working, unless it has been stuck too long.
    if _is_stuck(session):
        send_text(phone, _MSG_GENERATION_FAILED)
        _update_session_sync(supabase, session["id"], {"state": "ERROR"})


async def _watch_once() -> None:
    supabase = get_supabase_admin_client()
    sessions = await anyio.to_thread.run_sync(_fetch_processing_sessions_sync, supabase)

    for session in sessions:
        try:
            await anyio.to_thread.run_sync(_process_session_sync, supabase, session)
        except Exception as exc:
            print(f"[whatsapp_watcher] failed to process session {session.get('id')}: {exc}")
            traceback.print_exc()


async def _watch_loop() -> None:
    print("[whatsapp_watcher] started")
    while True:
        try:
            await _watch_once()
        except Exception as exc:
            print(f"[whatsapp_watcher] loop iteration failed: {exc}")
            traceback.print_exc()

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def start_completion_watcher() -> None:
    asyncio.create_task(_watch_loop())
