import base64
import time
import traceback
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from config import get_settings
from prompting import build_generation_prompt, build_tryon_prompt, build_tryon_quick_prompt
from supabase_client import get_supabase_admin_client
from tryon_api import (
    _call_gemini_tryon,
    _downscale_image_if_needed,
    _fetch_storage_bytes,
    _prepare_tryon_assets_sync,
)
from whatsapp_transport import (
    WhatsAppTransportError,
    download_media,
    send_image_by_media_id,
    send_text,
    upload_media,
)

_RESET_COMMANDS = {"reset", "restart", "start over"}
_MAX_MEDIA_BYTES = 8 * 1024 * 1024

_FABRIC_PROMPT_TEMPLATE = (
    "Photorealistic recreation of the garment shown in the reference photo, "
    "re-rendered in the provided fabric swatch. Front view, clean studio "
    "background. Preserve all garment construction details — seams, collar, "
    "buttons, lapels, and silhouette — exactly as shown in the reference photo."
)

_MSG_GREETING = (
    "Namaste {name}! 🙏 Main MyTryonAi hoon. Aapke fabric ko kisi bhi garment "
    "mein AI se dikha sakta hoon.\n\nShuru karne ke liye apne FABRIC (kapde) "
    "ki photo bhejiye 📸"
)
_MSG_RESET = "Theek hai, fresh start! Apne FABRIC (kapde) ki photo bhejiye 📸"
_MSG_ONLY_PHOTO = "Sirf photo bhejiye please 📸"
_MSG_WAITING_FABRIC_TEXT = (
    "Pehle apne fabric ki PHOTO bhejiye 📸 (Type 'reset' to start over)"
)
_MSG_WAITING_GARMENT_TEXT = (
    "Ab GARMENT ki reference photo bhejiye 👔 (Type 'reset' to start over)"
)
_MSG_FABRIC_RECEIVED = (
    "Fabric mil gaya ✅ Ab GARMENT ki photo bhejiye — jo design/style aap "
    "banana chahte ho (koi bhi suit, sherwani, shirt ki reference photo) 👔"
)
_MSG_PROCESSING = (
    "Aapka look ban raha hai... 🎨 2-3 minute lagenge. Ready hote hi yahin "
    "bhej dunga!"
)
_MSG_DELIVERED_TEXT = "Naya look banana ho to apne agle FABRIC ki photo bhejiye 📸"
_MSG_NO_CREDITS = (
    "Aapke 3 free looks complete ho gaye 🙏 Paid credits jald aa rahe hain — "
    "thoda intezaar kijiye, hum aapko yahin batayenge!"
)
_MSG_GENERATION_STARTED = (
    "Sab mil gaya! ✅ Aapka look ban raha hai 🎨 Usually 2-3 minute lagta hai. "
    "Ready hote hi photo yahin aa jayegi!"
)
_MSG_ERROR_RECOVERY = (
    "Maaf kijiye, kuch problem ho gayi thi 😔 Chaliye phir se shuru karte hain "
    "— apne FABRIC (kapde) ki photo bhejiye 📸"
)
_MSG_TECH_ERROR = (
    "Kuch technical problem aa gayi 😔 'reset' bhej ke dobara try kijiye."
)
_MSG_MEDIA_TOO_LARGE = "Yeh photo bahut badi hai. Chhoti size ki photo bhejiye please 📸"
_MSG_MEDIA_DOWNLOAD_FAILED = "Photo download nahi ho payi, dobara bhejiye please 📸"

_MSG_CHOOSE_MODE = (
    "Garment mil gaya ✅ Ab batayiye:\n\n"
    "1️⃣ LOOK — sirf garment ka AI look\n"
    "2️⃣ TRYON — apne CUSTOMER par pehna ke dikhao\n\n"
    "1 ya 2 bhejein."
)
_MSG_CHOOSE_MODE_REPEAT = (
    "1 ya 2 bhejein:\n\n"
    "1️⃣ LOOK — sirf garment ka AI look\n"
    "2️⃣ TRYON — apne CUSTOMER par pehna ke dikhao\n\n"
    "(Type 'reset' to start over)"
)
_MSG_CONSENT_ASK = (
    "Customer try-on ke liye unki permission zaroori hai 🙏 Kya customer ne "
    "apni photo use karne ki haan boli hai? Haan ho to YES bhejein."
)
_MSG_CONSENT_THANKS = "Dhanyavaad ✅ Ab customer ki photo bhejiye (saamne se, poori body) 📸"
_MSG_CONSENT_DECLINE = (
    "Customer ki permission ke bina hum aage nahi badh sakte 🙏 Jab unki haan "
    "ho jaaye, YES bhejein. Naya fabric try karna ho to fabric photo bhejiye."
)
_MSG_WAITING_CUSTOMER_PHOTO_TEXT = (
    "Customer ki photo bhejiye please (saamne se, poori body) 📸 "
    "(Type 'reset' to start over)"
)
_MSG_TRYON_PROCESSING = "Try-on ban raha hai... 🎨 1-2 minute!"
_MSG_TRYON_RECEIVED = "Customer photo mil gayi ✅ Try-on ban raha hai 🎨 1-2 minute!"
_MSG_TRYON_DELIVERED = (
    "Aapke customer ka look! 🤩✨\nNaya look banana ho to agla FABRIC bhejiye 📸"
)
_MSG_TRYON_FAILED = (
    "Try-on mein problem aa gayi 😔 Customer ki photo dobara bhejiye, ya naya "
    "fabric bhejein."
)
_MSG_OFFER_TRYON_REPEAT = (
    "Naya look ke liye apna agla FABRIC bhejiye, ya is look ko customer par "
    "dekhne ke liye TRYON likhiye 📸"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upload_bytes(supabase, *, bucket: str, path: str, data: bytes, content_type: str) -> None:
    options = {"content-type": content_type, "upsert": "true"}
    try:
        supabase.storage.from_(bucket).upload(path, data, options)
    except Exception:
        supabase.storage.from_(bucket).update(path, data, options)


def _get_shop_balance(supabase, shop_id: str) -> int:
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


def _update_session(supabase, session_id: str, payload: dict) -> None:
    payload = dict(payload)
    payload["last_message_at"] = _utc_now_iso()
    supabase.table("whatsapp_sessions").update(payload).eq("id", session_id).execute()


def _create_session_with_shadow_shop(supabase, phone: str, profile_name: Optional[str]) -> dict:
    shop_payload = {
        "name": f"WA +{phone}",
        "carousel_mode_default": False,
        "is_suspended": False,
    }
    try:
        shop_result = supabase.table("shops").insert(shop_payload).execute()
    except Exception as exc:
        if "is_suspended" in str(exc).lower():
            shop_payload.pop("is_suspended", None)
            shop_result = supabase.table("shops").insert(shop_payload).execute()
        else:
            raise

    shop_rows = getattr(shop_result, "data", None) or []
    if not shop_rows:
        raise RuntimeError("Failed to create shadow shop for WhatsApp session")
    shop_id = str(shop_rows[0]["id"])

    folder_payload = {
        "shop_id": shop_id,
        "name": "WhatsApp",
        "prompt_template": _FABRIC_PROMPT_TEMPLATE,
    }
    folder_result = supabase.table("hero_folders").insert(folder_payload).execute()
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise RuntimeError("Failed to create WhatsApp hero folder")
    folder_id = str(folder_rows[0]["id"])

    supabase.table("credit_ledger").insert(
        {
            "shop_id": shop_id,
            "delta": 3,
            "reason": "whatsapp_free_trial",
            "balance_after": 3,
        }
    ).execute()

    session_payload = {
        "phone_number": phone,
        "profile_name": profile_name,
        "shop_id": shop_id,
        "folder_id": folder_id,
        "state": "NEW",
    }
    session_result = supabase.table("whatsapp_sessions").insert(session_payload).execute()
    session_rows = getattr(session_result, "data", None) or []
    if not session_rows:
        raise RuntimeError("Failed to create WhatsApp session row")

    return session_rows[0]


def get_or_create_session(phone: str, profile_name: Optional[str]) -> dict:
    supabase = get_supabase_admin_client()

    result = (
        supabase.table("whatsapp_sessions")
        .select("*")
        .eq("phone_number", phone)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if rows:
        return rows[0]

    return _create_session_with_shadow_shop(supabase, phone, profile_name)


def _reset_to_waiting_fabric(supabase, session: dict, *, reply: str) -> None:
    _update_session(
        supabase,
        session["id"],
        {
            "state": "WAITING_FABRIC",
            "fabric_image_id": None,
            "hero_image_id": None,
            "active_generation_id": None,
            "error_detail": None,
        },
    )
    send_text(session["phone_number"], reply)


def _handle_fabric_image(supabase, session: dict, msg: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]
    media_id = msg.get("media_id")

    if not media_id:
        send_text(phone, _MSG_MEDIA_DOWNLOAD_FAILED)
        _update_session(supabase, session["id"], {})
        return

    try:
        content, mime_type = download_media(media_id)
    except WhatsAppTransportError as exc:
        print(f"[whatsapp_state] fabric media download failed: {exc}")
        send_text(phone, _MSG_MEDIA_DOWNLOAD_FAILED)
        _update_session(supabase, session["id"], {})
        return

    if len(content) > _MAX_MEDIA_BYTES:
        send_text(phone, _MSG_MEDIA_TOO_LARGE)
        _update_session(supabase, session["id"], {})
        return

    ts = int(time.time())
    storage_path = f"{shop_id}/wa-{ts}-{uuid4().hex[:8]}.jpg"

    _upload_bytes(
        supabase,
        bucket="fabric-images",
        path=storage_path,
        data=content,
        content_type=mime_type or "image/jpeg",
    )

    fabric_payload = {
        "shop_id": shop_id,
        "storage_path": storage_path,
        "original_filename": f"wa-fabric-{ts}",
        "mime_type": mime_type or "image/jpeg",
        "file_size_bytes": len(content),
        "width": None,
        "height": None,
    }
    fabric_result = supabase.table("fabric_images").insert(fabric_payload).execute()
    fabric_rows = getattr(fabric_result, "data", None) or []
    if not fabric_rows:
        raise RuntimeError("Failed to save fabric image metadata")
    fabric_image_id = str(fabric_rows[0]["id"])

    _update_session(
        supabase,
        session["id"],
        {
            "state": "WAITING_GARMENT",
            "fabric_image_id": fabric_image_id,
            "hero_image_id": None,
            "active_generation_id": None,
        },
    )
    send_text(phone, _MSG_FABRIC_RECEIVED)


def _handle_garment_image(supabase, session: dict, msg: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]
    folder_id = session["folder_id"]
    media_id = msg.get("media_id")

    if not media_id:
        send_text(phone, _MSG_MEDIA_DOWNLOAD_FAILED)
        _update_session(supabase, session["id"], {})
        return

    try:
        content, mime_type = download_media(media_id)
    except WhatsAppTransportError as exc:
        print(f"[whatsapp_state] garment media download failed: {exc}")
        send_text(phone, _MSG_MEDIA_DOWNLOAD_FAILED)
        _update_session(supabase, session["id"], {})
        return

    if len(content) > _MAX_MEDIA_BYTES:
        send_text(phone, _MSG_MEDIA_TOO_LARGE)
        _update_session(supabase, session["id"], {})
        return

    ts = int(time.time())
    storage_path = f"{shop_id}/wa-{ts}-{uuid4().hex[:8]}.jpg"

    _upload_bytes(
        supabase,
        bucket="hero-images",
        path=storage_path,
        data=content,
        content_type=mime_type or "image/jpeg",
    )

    hero_payload = {
        "shop_id": shop_id,
        "folder_id": folder_id,
        "storage_path": storage_path,
        "original_filename": f"wa-garment-{ts}",
        "mime_type": mime_type or "image/jpeg",
        "file_size_bytes": len(content),
        "width": None,
        "height": None,
    }
    hero_result = supabase.table("hero_images").insert(hero_payload).execute()
    hero_rows = getattr(hero_result, "data", None) or []
    if not hero_rows:
        raise RuntimeError("Failed to save hero image metadata")
    hero_image_id = str(hero_rows[0]["id"])

    _update_session(
        supabase,
        session["id"],
        {"state": "CHOOSING_MODE", "hero_image_id": hero_image_id},
    )
    send_text(phone, _MSG_CHOOSE_MODE)


def create_generation_for_session(supabase, session: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]
    hero_image_id = session.get("hero_image_id")
    fabric_image_id = session.get("fabric_image_id")

    balance = _get_shop_balance(supabase, shop_id)
    if balance < 1:
        send_text(phone, _MSG_NO_CREDITS)
        _update_session(supabase, session["id"], {"state": "DELIVERED"})
        return

    settings = get_settings()

    normalized_fabrics = [
        {
            "fabric_image_id": fabric_image_id,
            "apply_to": "suit_full_body",
            "fabric_code": "unknown",
            "fabric_color": None,
            "fabric_scale": None,
        }
    ]

    folder_result = (
        supabase.table("hero_folders")
        .select("id, name, prompt_template")
        .eq("id", session["folder_id"])
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise RuntimeError("WhatsApp folder not found")
    folder_context = folder_rows[0]

    prompt_used = build_generation_prompt(
        folder_name=folder_context.get("name"),
        folder_prompt_template=folder_context.get("prompt_template"),
        fabric_assignments=normalized_fabrics,
        fabric_scale=None,
    )

    result = (
        supabase.rpc(
            "create_generation_with_credit_debit_v2",
            {
                "p_shop_id": shop_id,
                "p_hero_image_id": hero_image_id,
                "p_fabrics": normalized_fabrics,
                "p_credits_cost": settings.CREDITS_PER_GENERATION,
            },
        )
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        raise RuntimeError("Generation RPC returned no rows")

    generation_id = str(rows[0]["generation_id"])

    (
        supabase.table("generations")
        .update({"prompt_used": prompt_used, "fabric_image_id": fabric_image_id})
        .eq("id", generation_id)
        .eq("shop_id", shop_id)
        .execute()
    )

    _update_session(
        supabase,
        session["id"],
        {"state": "PROCESSING", "active_generation_id": generation_id},
    )

    send_text(phone, _MSG_GENERATION_STARTED)


def _log_whatsapp_consent(supabase, shop_id: str) -> None:
    try:
        supabase.table("customer_consent_logs").insert(
            {
                "shop_id": shop_id,
                "purpose": "virtual_tryon_whatsapp",
                "confirmed_by_staff": False,
            }
        ).execute()
    except Exception as exc:
        print(f"[whatsapp_state] consent log insert failed: {exc}")


def _start_consent_flow(supabase, session: dict) -> None:
    send_text(session["phone_number"], _MSG_CONSENT_ASK)
    _update_session(supabase, session["id"], {"state": "WAITING_CONSENT"})


def _prepare_direct_tryon_assets(
    supabase, shop_id: str, hero_image_id: str, fabric_image_id: str
) -> tuple[bytes, str, bytes, str]:
    hero_result = (
        supabase.table("hero_images")
        .select("id, storage_path, mime_type")
        .eq("id", hero_image_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    hero_rows = getattr(hero_result, "data", None) or []
    if not hero_rows:
        raise RuntimeError("Hero image not found for direct try-on")
    hero = hero_rows[0]

    fabric_result = (
        supabase.table("fabric_images")
        .select("id, storage_path, mime_type")
        .eq("id", fabric_image_id)
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    fabric_rows = getattr(fabric_result, "data", None) or []
    if not fabric_rows:
        raise RuntimeError("Fabric image not found for direct try-on")
    fabric = fabric_rows[0]

    hero_bytes = _fetch_storage_bytes(supabase, "hero-images", hero["storage_path"])
    fabric_bytes = _fetch_storage_bytes(supabase, "fabric-images", fabric["storage_path"])

    return (
        hero_bytes,
        str(hero.get("mime_type") or "image/jpeg"),
        fabric_bytes,
        str(fabric.get("mime_type") or "image/jpeg"),
    )


def run_tryon_for_session(
    supabase, session: dict, media_id: Optional[str], mime_type: Optional[str]
) -> None:
    phone = session["phone_number"]
    session_id = session["id"]
    shop_id = session["shop_id"]

    _update_session(supabase, session_id, {"state": "TRYON_PROCESSING"})
    send_text(phone, _MSG_TRYON_RECEIVED)

    generation_id = session.get("active_generation_id")
    is_direct = not generation_id
    balance_before: Optional[int] = None

    try:
        if not media_id:
            raise RuntimeError("Missing media_id for customer photo")

        # Customer photo lives only in these local variables for the
        # duration of this call — never written to storage, DB, or logs.
        customer_bytes, downloaded_mime = download_media(media_id)
        customer_mime = downloaded_mime or mime_type or "image/jpeg"

        if len(customer_bytes) > _MAX_MEDIA_BYTES:
            raise RuntimeError("Customer photo too large")

        customer_bytes, customer_mime = _downscale_image_if_needed(customer_bytes, customer_mime)

        if is_direct:
            folder_result = (
                supabase.table("hero_folders")
                .select("id, name")
                .eq("id", session["folder_id"])
                .eq("shop_id", shop_id)
                .limit(1)
                .execute()
            )
            folder_rows = getattr(folder_result, "data", None) or []
            folder_name = folder_rows[0].get("name") if folder_rows else None

            hero_bytes, hero_mime, fabric_bytes, fabric_mime = _prepare_direct_tryon_assets(
                supabase, shop_id, session["hero_image_id"], session["fabric_image_id"]
            )
            hero_bytes, hero_mime = _downscale_image_if_needed(hero_bytes, hero_mime)
            fabric_bytes, fabric_mime = _downscale_image_if_needed(fabric_bytes, fabric_mime)

            balance_before = _get_shop_balance(supabase, shop_id)
            if balance_before < 1:
                send_text(phone, _MSG_NO_CREDITS)
                _update_session(supabase, session_id, {"state": "DELIVERED"})
                return

            prompt = build_tryon_quick_prompt(folder_name=folder_name)
            image_parts = [
                (hero_bytes, hero_mime),
                (fabric_bytes, fabric_mime),
                (customer_bytes, customer_mime),
            ]
        else:
            garment_bytes, folder_name = _prepare_tryon_assets_sync(supabase, shop_id, generation_id)
            garment_bytes, garment_mime = _downscale_image_if_needed(garment_bytes, "image/jpeg")

            prompt = build_tryon_prompt(folder_name=folder_name)
            image_parts = [
                (customer_bytes, customer_mime),
                (garment_bytes, garment_mime),
            ]

        result = _call_gemini_tryon(prompt=prompt, image_parts=image_parts)
        result_bytes = base64.b64decode(result.result_b64)

        result_media_id = upload_media(result_bytes, result.result_mime)
        send_image_by_media_id(phone, result_media_id, caption=_MSG_TRYON_DELIVERED)
    except Exception as exc:
        print(f"[whatsapp_state] tryon failed for session {session_id}: {exc}")
        send_text(phone, _MSG_TRYON_FAILED)
        _update_session(supabase, session_id, {"state": "WAITING_CUSTOMER_PHOTO"})
        return

    update_payload = {"state": "DELIVERED", "active_generation_id": None}

    if is_direct:
        new_balance = (balance_before or 0) - 1
        supabase.table("credit_ledger").insert(
            {
                "shop_id": shop_id,
                "delta": -1,
                "reason": "whatsapp_direct_tryon",
                "balance_after": new_balance,
            }
        ).execute()
        update_payload["free_generations_used"] = int(session.get("free_generations_used") or 0) + 1

    _update_session(supabase, session_id, update_payload)


def _dispatch(supabase, session: dict, msg: dict) -> None:
    phone = session["phone_number"]
    session_id = session["id"]
    kind = msg.get("kind")
    text = (msg.get("text") or "").strip()
    state = session.get("state") or "NEW"

    if kind == "text" and text.lower() in _RESET_COMMANDS:
        _reset_to_waiting_fabric(supabase, session, reply=_MSG_RESET)
        return

    if state == "NEW":
        name = session.get("profile_name") or msg.get("profile_name") or "dost"
        send_text(phone, _MSG_GREETING.format(name=name))
        _update_session(supabase, session_id, {"state": "WAITING_FABRIC"})
        return

    if state == "WAITING_FABRIC":
        if kind == "image":
            _handle_fabric_image(supabase, session, msg)
        elif kind == "text":
            send_text(phone, _MSG_WAITING_FABRIC_TEXT)
            _update_session(supabase, session_id, {})
        else:
            send_text(phone, _MSG_ONLY_PHOTO)
            _update_session(supabase, session_id, {})
        return

    if state == "WAITING_GARMENT":
        if kind == "image":
            _handle_garment_image(supabase, session, msg)
        elif kind == "text":
            send_text(phone, _MSG_WAITING_GARMENT_TEXT)
            _update_session(supabase, session_id, {})
        else:
            send_text(phone, _MSG_ONLY_PHOTO)
            _update_session(supabase, session_id, {})
        return

    if state == "CHOOSING_MODE":
        normalized = text.lower()
        if kind == "text" and normalized in {"1", "look"}:
            create_generation_for_session(supabase, session)
        elif kind == "text" and normalized in {"2", "tryon", "try on"}:
            _start_consent_flow(supabase, session)
        else:
            send_text(phone, _MSG_CHOOSE_MODE_REPEAT)
            _update_session(supabase, session_id, {})
        return

    if state == "WAITING_CONSENT":
        normalized = text.lower()
        if kind == "text" and normalized in {"yes", "haan"}:
            _log_whatsapp_consent(supabase, session["shop_id"])
            send_text(phone, _MSG_CONSENT_THANKS)
            _update_session(supabase, session_id, {"state": "WAITING_CUSTOMER_PHOTO"})
        elif kind == "image":
            _handle_fabric_image(supabase, session, msg)
        else:
            send_text(phone, _MSG_CONSENT_DECLINE)
            _update_session(supabase, session_id, {})
        return

    if state == "WAITING_CUSTOMER_PHOTO":
        if kind == "image":
            run_tryon_for_session(supabase, session, msg.get("media_id"), msg.get("mime_type"))
        elif kind == "text":
            send_text(phone, _MSG_WAITING_CUSTOMER_PHOTO_TEXT)
            _update_session(supabase, session_id, {})
        else:
            send_text(phone, _MSG_ONLY_PHOTO)
            _update_session(supabase, session_id, {})
        return

    if state == "TRYON_PROCESSING":
        send_text(phone, _MSG_TRYON_PROCESSING)
        _update_session(supabase, session_id, {})
        return

    if state == "OFFER_TRYON":
        normalized = text.lower()
        if kind == "text" and normalized in {"tryon", "try on"}:
            _start_consent_flow(supabase, session)
        elif kind == "image":
            _handle_fabric_image(supabase, session, msg)
        else:
            send_text(phone, _MSG_OFFER_TRYON_REPEAT)
            _update_session(supabase, session_id, {})
        return

    if state == "PROCESSING":
        send_text(phone, _MSG_PROCESSING)
        _update_session(supabase, session_id, {})
        return

    if state == "DELIVERED":
        if kind == "image":
            _handle_fabric_image(supabase, session, msg)
        elif kind == "text":
            send_text(phone, _MSG_DELIVERED_TEXT)
            _update_session(supabase, session_id, {})
        else:
            send_text(phone, _MSG_ONLY_PHOTO)
            _update_session(supabase, session_id, {})
        return

    if state == "ERROR":
        _reset_to_waiting_fabric(supabase, session, reply=_MSG_ERROR_RECOVERY)
        return

    # Unknown/legacy state: recover into a known-good state.
    _reset_to_waiting_fabric(supabase, session, reply=_MSG_ERROR_RECOVERY)


def handle_incoming(msg: dict) -> None:
    phone = msg.get("from_phone")
    if not phone:
        return

    supabase = get_supabase_admin_client()

    try:
        session = get_or_create_session(phone, msg.get("profile_name"))
    except Exception as exc:
        print(f"[whatsapp_state] failed to get/create session for {phone}: {exc}")
        traceback.print_exc()
        send_text(phone, _MSG_TECH_ERROR)
        return

    try:
        _dispatch(supabase, session, msg)
    except Exception as exc:
        print(f"[whatsapp_state] unhandled error for {phone}: {exc}")
        traceback.print_exc()
        try:
            _update_session(
                supabase,
                session["id"],
                {"state": "ERROR", "error_detail": str(exc)[:1500]},
            )
        except Exception:
            traceback.print_exc()
        send_text(phone, _MSG_TECH_ERROR)
