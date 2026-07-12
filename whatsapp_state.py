import time
import traceback
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from config import get_settings
from prompting import build_generation_prompt
from supabase_client import get_supabase_admin_client
from whatsapp_transport import WhatsAppTransportError, download_media, send_text

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

    _update_session(supabase, session["id"], {"hero_image_id": hero_image_id})

    session = dict(session)
    session["hero_image_id"] = hero_image_id
    create_generation_for_session(supabase, session)


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
