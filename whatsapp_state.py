import base64
import random
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from config import get_settings
from payments_api import CREDIT_PACKS, create_payment_link_for_shop
from prompting import DEFAULT_LOOK_PROMPT, DEFAULT_TRYON_PROMPT, fill_prompt_placeholders
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
    send_interactive_list,
    send_text,
    upload_media,
)

_RESET_COMMANDS = {"reset", "restart", "start over"}
_BUY_PACK_ID = "starter"
_MAX_MEDIA_BYTES = 8 * 1024 * 1024

# Unambiguous alphabet for join codes — excludes O/0 and I/1, which are easy
# to mis-type or mis-read when a shop owner reads a code aloud.
_JOIN_CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_JOIN_CODE_DIGITS = "23456789"
_JOIN_COMMAND_RE = re.compile(r"^join\s+([a-z0-9\s]+)$", re.IGNORECASE)

_MASTER_TEMPLATE_CACHE_TTL_SECONDS = 300
_master_template_cache: dict = {"data": None, "loaded_at": 0.0}

_FABRIC_PROMPT_TEMPLATE = (
    "Photorealistic recreation of the garment shown in the reference photo, "
    "re-rendered in the provided fabric swatch. Front view, clean studio "
    "background. Preserve all garment construction details — seams, collar, "
    "buttons, lapels, and silhouette — exactly as shown in the reference photo."
)

_MSG_GREETING = (
    "Namaste {name}! 🙏 Main MyTryonAi hoon. Aapke fabric ko kisi bhi garment "
    "mein AI se dikha sakta hoon."
)
_MSG_RESET = "Theek hai, fresh start! 🔄"
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
_MSG_GARMENT_RECEIVED_NEED_FABRIC = "Garment mil gaya ✅ Ab apne FABRIC ki photo bhejiye 📸"

_MSG_GARMENT_MENU_HEADER = "Garment Type"
_MSG_GARMENT_MENU_BODY = (
    "Konsa garment banwana hai? Neeche list se choose karein 👇\n\n"
    "Apna khud ka garment chahiye? 'custom' likhein."
)
_MSG_GARMENT_MENU_BUTTON = "Choose"
_MSG_GARMENT_MENU_INVALID = (
    "Sahi option list se choose karein, ya number bhejein 👆 "
    "(Apna khud ka garment chahiye to 'custom' likhein)"
)
_MSG_GARMENT_CHOSEN_TEMPLATE = "{garment} select ho gaya ✅ Ab apne FABRIC ki photo bhejiye 📸"
_MSG_MULTIFABRIC_PROMPT = (
    "Fabric #{n} of {total} bhejiye — {label} ke liye 📸 (Type 'reset' to start over)"
)
_MSG_ENHANCED_LOCKED = (
    "Custom garment design Enhanced plan mein milta hai ⭐ Interested ho to "
    "reply karein, hum aapko details bhejenge!"
)
_MSG_ENHANCED_UNLOCKED = "Apne GARMENT ki reference photo bhejiye 👔"
_MSG_MENU_HINT_SUFFIX = "\n\nDoosra garment chahiye? 'menu' bhejein."

_MSG_PROCESSING = (
    "Aapka look ban raha hai... 🎨 2-3 minute lagenge. Ready hote hi yahin "
    "bhej dunga!"
)
_MSG_DELIVERED_TEXT = (
    "Naya look banana ho to apne agle FABRIC ki photo bhejiye 📸 "
    "(Ya 'menu' bhejein doosra garment choose karne ke liye)"
)
_BUY_PACK = CREDIT_PACKS[_BUY_PACK_ID]
_BUY_PACK_IMAGES = _BUY_PACK["images"]
_BUY_PACK_RUPEES = _BUY_PACK["amount_paise"] // 100

_MSG_NO_CREDITS = (
    f"Aapke free looks complete ho gaye 🎉\n\n"
    f"{_BUY_PACK_IMAGES} aur looks sirf Rs.{_BUY_PACK_RUPEES} mein! "
    "Lene ke liye 'BUY' likhein 💳"
)
_MSG_BUY_OFFER_TEMPLATE = (
    "{images} looks ka pack - sirf Rs.{rupees} 💳\n\n"
    "Yahan pay karein:\n{short_url}\n\n"
    "Payment ke baad looks turant add ho jaayenge!"
)
_MSG_BUY_FAILED = "Payment link banane mein problem 😔 Thodi der baad 'buy' bhejein."
_MSG_GENERATION_STARTED = (
    "Sab mil gaya! ✅ Aapka look ban raha hai 🎨 Usually 2-3 minute lagta hai. "
    "Ready hote hi photo yahin aa jayegi!"
)
_MSG_ERROR_RECOVERY = (
    "Maaf kijiye, kuch problem ho gayi thi 😔 Chaliye phir se shuru karte hain."
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
_MSG_TRYON_STILL_WORKING = "Bas thodi der aur 🙏 Aapke customer ka try-on tayaar ho raha hai... ✨"
_MSG_TRYON_DELIVERED = (
    "Aapke customer ka look! 🤩✨\nNaya look banana ho to agla FABRIC bhejiye 📸"
)
_MSG_TRYON_FAILED = (
    "Try-on mein problem aa gayi 😔 Customer ki photo dobara bhejiye, ya naya "
    "fabric bhejein."
)
_MSG_OFFER_TRYON_REPEAT = (
    "Naya look ke liye apna agla FABRIC bhejiye, is look ko customer par "
    "dekhne ke liye TRYON likhiye, ya doosra garment ke liye 'menu' bhejein 📸"
)

_MSG_TEAM_INVITE_TEMPLATE = (
    "Apni team ko isi number par bhejwayein:\n\n"
    "JOIN {code}\n\n"
    "Sab ek hi credit pool use karenge. Aap owner rahenge — recharge sirf "
    "aapke bolne par 👥"
)
_MSG_JOIN_NOT_FOUND = "Ye code sahi nahi lag raha 🤔 Shop owner se dobara confirm karein."
_MSG_JOIN_ALREADY_MEMBER = "Aap pehle se isi team mein hain 👍"
_MSG_JOIN_SUCCESS_TEMPLATE = (
    "Aap {shop_name} ki team se jud gaye! 🎉 Ab aapke looks shop ke shared "
    "credits se banenge."
)

_MSG_TV_PUSHED = "Bade screen par bhej diya 📺✨"
_MSG_TV_NONE = "Pehle ek look banaiye, phir TV likhein 🙂"
_MSG_TV_CLEARED = "Screen wapas catalog par aa gaya ✅"

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


def _get_shop_multifabric_enabled(supabase, shop_id: str) -> bool:
    try:
        result = (
            supabase.table("shops")
            .select("whatsapp_multifabric_enabled")
            .eq("id", shop_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        print(f"[whatsapp_state] failed to load multifabric flag for shop {shop_id}: {exc}")
        return False

    rows = getattr(result, "data", None) or []
    if not rows:
        return False
    return bool(rows[0].get("whatsapp_multifabric_enabled") or False)


def _load_fabric_slots(supabase, folder_id: str) -> list[dict]:
    try:
        result = (
            supabase.table("garment_fabric_slots")
            .select("label, apply_to, sort_order")
            .eq("folder_id", folder_id)
            .order("sort_order")
            .limit(6)
            .execute()
        )
    except Exception as exc:
        print(f"[whatsapp_state] failed to load fabric slots for folder {folder_id}: {exc}")
        return []

    return getattr(result, "data", None) or []


def _update_session(supabase, session_id: str, payload: dict) -> None:
    payload = dict(payload)
    payload["last_message_at"] = _utc_now_iso()
    supabase.table("whatsapp_sessions").update(payload).eq("id", session_id).execute()


def _load_master_templates(supabase) -> list[dict]:
    settings = get_settings()
    master_shop_id = settings.MASTER_SHOP_ID
    if not master_shop_id:
        raise RuntimeError("MASTER_SHOP_ID is not configured")

    folder_result = (
        supabase.table("garment_types")
        .select("id, name")
        .eq("shop_id", master_shop_id)
        .eq("is_active", True)
        .order("name")
        .execute()
    )
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        return []

    folder_ids = [str(row["id"]) for row in folder_rows]
    hero_result = (
        supabase.table("hero_images")
        .select("id, folder_id, storage_path, mime_type")
        .eq("shop_id", master_shop_id)
        .in_("folder_id", folder_ids)
        .execute()
    )
    hero_rows = getattr(hero_result, "data", None) or []
    hero_by_folder = {str(row["folder_id"]): row for row in hero_rows}

    templates: list[dict] = []
    for folder in folder_rows:
        folder_id = str(folder["id"])
        hero = hero_by_folder.get(folder_id)
        if not hero:
            # Folder has no hero image yet — not selectable in the menu.
            continue
        templates.append(
            {
                "folder_id": folder_id,
                "name": str(folder["name"]),
                "hero_storage_path": str(hero["storage_path"]),
                "hero_mime_type": str(hero.get("mime_type") or "image/jpeg"),
            }
        )

    return templates


def get_master_templates(supabase) -> list[dict]:
    """Master garment-type list for the BASIC menu, cached in memory with a
    short TTL so new garment types added to the master shop show up without
    a redeploy."""
    now = time.time()
    cached = _master_template_cache["data"]
    if cached is not None and (now - _master_template_cache["loaded_at"]) < _MASTER_TEMPLATE_CACHE_TTL_SECONDS:
        return cached

    templates = _load_master_templates(supabase)
    _master_template_cache["data"] = templates
    _master_template_cache["loaded_at"] = now
    return templates


def _resolve_shadow_hero_image(supabase, *, shop_id: str, folder_id: str, template: dict) -> str:
    """fetch_generation_assets in worker.py scopes hero_images lookups by
    shop_id, so a shadow-shop generation cannot reference the master shop's
    hero_image_id directly. Lazily copy the row (same storage_path, no file
    copy) into the shadow shop the first time a garment type is picked, and
    reuse it by storage_path afterwards."""
    storage_path = template["hero_storage_path"]

    existing = (
        supabase.table("hero_images")
        .select("id")
        .eq("shop_id", shop_id)
        .eq("storage_path", storage_path)
        .limit(1)
        .execute()
    )
    existing_rows = getattr(existing, "data", None) or []
    if existing_rows:
        return str(existing_rows[0]["id"])

    payload = {
        "shop_id": shop_id,
        "folder_id": folder_id,
        "storage_path": storage_path,
        "original_filename": f"master-{template['name']}",
        "mime_type": template.get("hero_mime_type") or "image/jpeg",
        "file_size_bytes": None,
        "width": None,
        "height": None,
    }
    result = supabase.table("hero_images").insert(payload).execute()
    rows = getattr(result, "data", None) or []
    if not rows:
        raise RuntimeError("Failed to copy master hero image into shadow shop")
    return str(rows[0]["id"])


def _load_own_menu_templates(supabase, shop_id: str) -> list[dict]:
    """A shop's own WhatsApp-menu garment types, if any are flagged. These
    already have real hero_images rows in the shop, so no copy trick is
    needed — unlike master templates, which are only borrowed by reference."""
    try:
        folder_result = (
            supabase.table("garment_types")
            .select("id, name, default_hero_image_id")
            .eq("shop_id", shop_id)
            .eq("is_active", True)
            .eq("show_in_whatsapp_menu", True)
            .order("name")
            .execute()
        )
        folder_rows = getattr(folder_result, "data", None) or []
        if not folder_rows:
            return []

        folder_ids = [str(row["id"]) for row in folder_rows]
        hero_result = (
            supabase.table("hero_images")
            .select("id, folder_id, storage_path, mime_type, created_at")
            .eq("shop_id", shop_id)
            .in_("folder_id", folder_ids)
            .order("created_at", desc=True)
            .execute()
        )
        hero_rows = getattr(hero_result, "data", None) or []

        heroes_by_id = {str(row["id"]): row for row in hero_rows}
        latest_hero_by_folder: dict = {}
        for row in hero_rows:
            folder_id = str(row["folder_id"])
            if folder_id not in latest_hero_by_folder:
                latest_hero_by_folder[folder_id] = row

        templates: list[dict] = []
        for folder in folder_rows:
            folder_id = str(folder["id"])
            default_hero_id = folder.get("default_hero_image_id")
            hero = heroes_by_id.get(str(default_hero_id)) if default_hero_id else None
            if hero is None:
                hero = latest_hero_by_folder.get(folder_id)
            if hero is None:
                # No hero image at all for this garment type — not selectable.
                continue

            templates.append(
                {
                    "folder_id": folder_id,
                    "name": str(folder["name"]),
                    "hero_image_id": str(hero["id"]),
                    "hero_storage_path": str(hero["storage_path"]),
                    "hero_mime_type": str(hero.get("mime_type") or "image/jpeg"),
                    "is_own": True,
                }
            )

        return templates
    except Exception as exc:
        print(f"[whatsapp_state] failed to load own menu templates for shop {shop_id}: {exc}")
        return []


def _get_menu_templates(supabase, session: dict) -> tuple[list[dict], bool]:
    own = _load_own_menu_templates(supabase, session["shop_id"])
    if own:
        return own, True
    return get_master_templates(supabase), False


def _send_garment_menu(supabase, session: dict, *, extra_fields: Optional[dict] = None) -> None:
    phone = session["phone_number"]
    templates, _is_own = _get_menu_templates(supabase, session)
    rows = [{"id": t["folder_id"], "title": t["name"]} for t in templates]

    send_interactive_list(
        phone,
        header=_MSG_GARMENT_MENU_HEADER,
        body=_MSG_GARMENT_MENU_BODY,
        button_label=_MSG_GARMENT_MENU_BUTTON,
        rows=rows,
    )

    payload = {"state": "CHOOSING_GARMENT"}
    if extra_fields:
        payload.update(extra_fields)
    _update_session(supabase, session["id"], payload)


def _generate_join_code() -> str:
    letters = "".join(random.choice(_JOIN_CODE_LETTERS) for _ in range(4))
    digits = "".join(random.choice(_JOIN_CODE_DIGITS) for _ in range(2))
    return letters + digits


def get_or_create_join_code(supabase, shop_id: str) -> str:
    result = (
        supabase.table("shops")
        .select("whatsapp_join_code")
        .eq("id", shop_id)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    existing = rows[0].get("whatsapp_join_code") if rows else None
    if existing:
        return str(existing)

    for _ in range(10):
        code = _generate_join_code()
        try:
            supabase.table("shops").update({"whatsapp_join_code": code}).eq("id", shop_id).execute()
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                continue
            raise
        return code

    raise RuntimeError("Failed to generate a unique WhatsApp join code")


def _get_or_create_whatsapp_folder(supabase, shop_id: str) -> str:
    result = (
        supabase.table("garment_types")
        .select("id")
        .eq("shop_id", shop_id)
        .eq("name", "WhatsApp")
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if rows:
        return str(rows[0]["id"])

    folder_payload = {
        "shop_id": shop_id,
        "name": "WhatsApp",
        "prompt_template": _FABRIC_PROMPT_TEMPLATE,
    }
    folder_result = supabase.table("garment_types").insert(folder_payload).execute()
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise RuntimeError("Failed to create WhatsApp hero folder")
    return str(folder_rows[0]["id"])


def _create_session_with_shadow_shop(supabase, phone: str, profile_name: Optional[str]) -> dict:
    shop_payload = {
        "name": f"WA +{phone}",
        "carousel_mode_default": False,
        "is_suspended": False,
        "status": "trial",
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

    folder_id = _get_or_create_whatsapp_folder(supabase, shop_id)

    settings = get_settings()
    free_trial_credits = settings.WHATSAPP_FREE_IMAGES * settings.CREDITS_PER_IMAGE
    supabase.table("credit_ledger").insert(
        {
            "shop_id": shop_id,
            "delta": free_trial_credits,
            "reason": "whatsapp_free_trial",
            "balance_after": free_trial_credits,
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


def _reset_to_choosing_garment(supabase, session: dict, *, reply: str) -> None:
    send_text(session["phone_number"], reply)
    _send_garment_menu(
        supabase,
        session,
        extra_fields={
            "fabric_image_id": None,
            "hero_image_id": None,
            "active_generation_id": None,
            "error_detail": None,
            "pending_fabrics": None,
        },
    )


def _handle_fabric_image(
    supabase, session: dict, msg: dict, *, mode_menu_suffix: str = ""
) -> None:
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

    if session.get("hero_image_id"):
        # Garment already resolved (BASIC menu pick or a completed ENHANCED
        # custom-garment upload) — skip straight to the mode menu.
        _update_session(
            supabase,
            session["id"],
            {
                "state": "CHOOSING_MODE",
                "fabric_image_id": fabric_image_id,
                "active_generation_id": None,
            },
        )
        send_text(phone, _MSG_CHOOSE_MODE + mode_menu_suffix)
    else:
        _update_session(
            supabase,
            session["id"],
            {
                "state": "WAITING_GARMENT",
                "fabric_image_id": fabric_image_id,
                "active_generation_id": None,
            },
        )
        send_text(phone, _MSG_FABRIC_RECEIVED)


def _handle_collecting_fabrics(supabase, session: dict, msg: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]
    session_id = session["id"]
    kind = msg.get("kind")

    slots = _load_fabric_slots(supabase, session["folder_id"])
    pending = session.get("pending_fabrics") or []
    index = len(pending)

    if index >= len(slots):
        # Safety: somehow already have enough fabrics — treat collection as complete.
        _update_session(
            supabase,
            session_id,
            {
                "state": "CHOOSING_MODE",
                "pending_fabrics": pending,
                "fabric_image_id": pending[0]["fabric_image_id"],
                "active_generation_id": None,
            },
        )
        send_text(phone, _MSG_CHOOSE_MODE)
        return

    current_slot = slots[index]

    if kind != "image":
        if kind == "text":
            send_text(
                phone,
                _MSG_MULTIFABRIC_PROMPT.format(n=index + 1, total=len(slots), label=current_slot["label"]),
            )
        else:
            send_text(phone, _MSG_ONLY_PHOTO)
        _update_session(supabase, session_id, {})
        return

    media_id = msg.get("media_id")
    if not media_id:
        send_text(phone, _MSG_MEDIA_DOWNLOAD_FAILED)
        _update_session(supabase, session_id, {})
        return

    try:
        content, mime_type = download_media(media_id)
    except WhatsAppTransportError as exc:
        print(f"[whatsapp_state] multifabric media download failed: {exc}")
        send_text(phone, _MSG_MEDIA_DOWNLOAD_FAILED)
        _update_session(supabase, session_id, {})
        return

    if len(content) > _MAX_MEDIA_BYTES:
        send_text(phone, _MSG_MEDIA_TOO_LARGE)
        _update_session(supabase, session_id, {})
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

    pending = pending + [{"fabric_image_id": fabric_image_id, "apply_to": current_slot["apply_to"]}]
    next_index = index + 1

    if next_index < len(slots):
        _update_session(supabase, session_id, {"pending_fabrics": pending})
        send_text(
            phone,
            _MSG_MULTIFABRIC_PROMPT.format(n=next_index + 1, total=len(slots), label=slots[next_index]["label"]),
        )
        return

    _update_session(
        supabase,
        session_id,
        {
            "state": "CHOOSING_MODE",
            "pending_fabrics": pending,
            "fabric_image_id": pending[0]["fabric_image_id"],
            "active_generation_id": None,
        },
    )
    send_text(phone, _MSG_CHOOSE_MODE)


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

    if session.get("fabric_image_id"):
        _update_session(
            supabase,
            session["id"],
            {"state": "CHOOSING_MODE", "hero_image_id": hero_image_id},
        )
        send_text(phone, _MSG_CHOOSE_MODE)
    else:
        # ENHANCED entry: garment reference is resolved before fabric —
        # a generation needs both, so collect fabric next rather than
        # jumping straight to the mode menu.
        _update_session(
            supabase,
            session["id"],
            {"state": "WAITING_FABRIC", "hero_image_id": hero_image_id},
        )
        send_text(phone, _MSG_GARMENT_RECEIVED_NEED_FABRIC)


def create_generation_for_session(supabase, session: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]
    hero_image_id = session.get("hero_image_id")
    fabric_image_id = session.get("fabric_image_id")

    settings = get_settings()
    balance = _get_shop_balance(supabase, shop_id)
    if balance < settings.CREDITS_PER_IMAGE:
        send_text(phone, _MSG_NO_CREDITS)
        _update_session(supabase, session["id"], {"state": "DELIVERED"})
        return

    pending_fabrics = session.get("pending_fabrics") or []
    if pending_fabrics:
        normalized_fabrics = [
            {
                "fabric_image_id": item["fabric_image_id"],
                "apply_to": item["apply_to"],
                "fabric_code": "unknown",
                "fabric_color": None,
                "fabric_scale": None,
            }
            for item in pending_fabrics
        ]
    else:
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
        supabase.table("garment_types")
        .select("id, name, look_prompt")
        .eq("id", session["folder_id"])
        .eq("shop_id", shop_id)
        .limit(1)
        .execute()
    )
    folder_rows = getattr(folder_result, "data", None) or []
    if not folder_rows:
        raise RuntimeError("WhatsApp folder not found")
    folder_context = folder_rows[0]

    look_prompt = folder_context.get("look_prompt")
    if not look_prompt or not look_prompt.strip():
        print(
            f"[whatsapp_state] WARNING: garment {folder_context.get('id')} has no "
            "look_prompt configured; using fallback prompt"
        )
        look_prompt = DEFAULT_LOOK_PROMPT

    prompt_used = fill_prompt_placeholders(
        look_prompt,
        garment_name=folder_context.get("name"),
        fabric_assignments=normalized_fabrics,
        image_count=1 + len(normalized_fabrics),
    )

    result = (
        supabase.rpc(
            "create_generation_with_credit_debit_v2",
            {
                "p_shop_id": shop_id,
                "p_hero_image_id": hero_image_id,
                "p_fabrics": normalized_fabrics,
                "p_credits_cost": settings.CREDITS_PER_IMAGE,
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
        .update(
            {
                "prompt_used": prompt_used,
                "fabric_image_id": fabric_image_id,
                "generation_type": "look",
                "model_used": settings.GEMINI_IMAGE_MODEL_ID,
            }
        )
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
    supabase, shop_id: str, hero_image_id: str, fabric_image_ids: list[str]
) -> tuple[bytes, str, list[tuple[bytes, str]]]:
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

    fabric_parts: list[tuple[bytes, str]] = []
    for fabric_image_id in fabric_image_ids:
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
        fabric_bytes = _fetch_storage_bytes(supabase, "fabric-images", fabric["storage_path"])
        fabric_parts.append((fabric_bytes, str(fabric.get("mime_type") or "image/jpeg")))

    hero_bytes = _fetch_storage_bytes(supabase, "hero-images", hero["storage_path"])

    return (
        hero_bytes,
        str(hero.get("mime_type") or "image/jpeg"),
        fabric_parts,
    )


def _record_tryon_generation(
    supabase,
    *,
    session: dict,
    is_direct: bool,
    source_generation_id: Optional[str],
    result_bytes: bytes,
    result_mime: str,
    credits_used: int,
    prompt_used: str,
) -> Optional[str]:
    """Record a generations row for a WhatsApp try-on OUTPUT image, mirroring
    the LOOK path, so the try-on is discoverable (TV screen, history) by
    output_path. The customer's input selfie is never stored — only this
    generated result image."""
    shop_id = session["shop_id"]

    if is_direct:
        hero_image_id = session.get("hero_image_id")
        folder_id = session.get("folder_id")
        pending_fabrics = session.get("pending_fabrics") or []
        fabric_image_id = (
            pending_fabrics[0]["fabric_image_id"]
            if pending_fabrics
            else session.get("fabric_image_id")
        )
    else:
        source_result = (
            supabase.table("generations")
            .select("hero_image_id, fabric_image_id, folder_id")
            .eq("id", source_generation_id)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
        source_rows = getattr(source_result, "data", None) or []
        source = source_rows[0] if source_rows else {}
        hero_image_id = source.get("hero_image_id")
        folder_id = source.get("folder_id")
        fabric_image_id = source.get("fabric_image_id")

    new_generation_id = str(uuid4())
    ext = "png" if "png" in (result_mime or "").lower() else "jpg"
    output_path = f"{shop_id}/{new_generation_id}/output_v{int(time.time())}.{ext}"

    _upload_bytes(
        supabase,
        bucket="generated-outputs",
        path=output_path,
        data=result_bytes,
        content_type=result_mime or "image/jpeg",
    )

    now_iso = _utc_now_iso()
    supabase.table("generations").insert(
        {
            "id": new_generation_id,
            "shop_id": shop_id,
            "hero_image_id": hero_image_id,
            "fabric_image_id": fabric_image_id,
            "folder_id": folder_id,
            "status": "done",
            "generation_type": "tryon",
            "model_used": get_settings().GEMINI_IMAGE_MODEL_ID,
            "prompt_used": prompt_used,
            "output_path": output_path,
            "credits_used": credits_used,
            "created_at": now_iso,
            "started_at": now_iso,
            "completed_at": now_iso,
        }
    ).execute()

    return new_generation_id


def run_tryon_for_session(
    supabase, session: dict, media_id: Optional[str], mime_type: Optional[str]
) -> None:
    phone = session["phone_number"]
    session_id = session["id"]
    shop_id = session["shop_id"]
    settings = get_settings()

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
                supabase.table("garment_types")
                .select("id, name, tryon_prompt")
                .eq("id", session["folder_id"])
                .eq("shop_id", shop_id)
                .limit(1)
                .execute()
            )
            folder_rows = getattr(folder_result, "data", None) or []
            folder_row = folder_rows[0] if folder_rows else {}
            folder_name = folder_row.get("name")

            pending_fabrics = session.get("pending_fabrics") or []
            fabric_image_ids = (
                [item["fabric_image_id"] for item in pending_fabrics]
                if pending_fabrics
                else [session["fabric_image_id"]]
            )
            fabric_assignments = (
                [{"apply_to": item["apply_to"]} for item in pending_fabrics]
                if pending_fabrics
                else [{"apply_to": "suit_full_body"}]
            )

            hero_bytes, hero_mime, fabric_parts = _prepare_direct_tryon_assets(
                supabase, shop_id, session["hero_image_id"], fabric_image_ids
            )
            hero_bytes, hero_mime = _downscale_image_if_needed(hero_bytes, hero_mime)
            fabric_parts = [
                _downscale_image_if_needed(fabric_bytes, fabric_mime)
                for fabric_bytes, fabric_mime in fabric_parts
            ]

            balance_before = _get_shop_balance(supabase, shop_id)
            if balance_before < settings.CREDITS_PER_IMAGE:
                send_text(phone, _MSG_NO_CREDITS)
                _update_session(supabase, session_id, {"state": "DELIVERED"})
                return

            tryon_prompt = folder_row.get("tryon_prompt")
            if not tryon_prompt or not tryon_prompt.strip():
                print(
                    f"[whatsapp_state] WARNING: garment {folder_row.get('id')} has no "
                    "tryon_prompt configured; using fallback prompt"
                )
                tryon_prompt = DEFAULT_TRYON_PROMPT

            prompt = fill_prompt_placeholders(
                tryon_prompt,
                garment_name=folder_name,
                fabric_assignments=fabric_assignments,
                image_count=1 + len(fabric_image_ids) + 1,
            )
            image_parts = [
                (hero_bytes, hero_mime),
                *fabric_parts,
                (customer_bytes, customer_mime),
            ]
        else:
            garment_bytes, folder_name, tryon_prompt = _prepare_tryon_assets_sync(
                supabase, shop_id, generation_id
            )
            garment_bytes, garment_mime = _downscale_image_if_needed(garment_bytes, "image/jpeg")

            if not tryon_prompt or not tryon_prompt.strip():
                print(
                    f"[whatsapp_state] WARNING: generation {generation_id}'s garment has no "
                    "tryon_prompt configured; using fallback prompt"
                )
                tryon_prompt = DEFAULT_TRYON_PROMPT

            prompt = fill_prompt_placeholders(
                tryon_prompt,
                garment_name=folder_name,
                fabric_assignments=None,
                image_count=2,
            )
            image_parts = [
                (customer_bytes, customer_mime),
                (garment_bytes, garment_mime),
            ]

        still_working_timer = threading.Timer(25.0, send_text, args=(phone, _MSG_TRYON_STILL_WORKING))
        still_working_timer.start()
        try:
            result = _call_gemini_tryon(prompt=prompt, image_parts=image_parts)
        finally:
            still_working_timer.cancel()
        result_bytes = base64.b64decode(result.result_b64)

        result_media_id = upload_media(result_bytes, result.result_mime)
        send_image_by_media_id(phone, result_media_id, caption=_MSG_TRYON_DELIVERED)

        tryon_generation_id: Optional[str] = None
        try:
            tryon_generation_id = _record_tryon_generation(
                supabase,
                session=session,
                is_direct=is_direct,
                source_generation_id=generation_id,
                result_bytes=result_bytes,
                result_mime=result.result_mime,
                credits_used=settings.CREDITS_PER_IMAGE if is_direct else 0,
                prompt_used=prompt,
            )
        except Exception as exc:
            print(f"[whatsapp_state] failed to record generations row for tryon session {session_id}: {exc}")
    except Exception as exc:
        print(f"[whatsapp_state] tryon failed for session {session_id}: {exc}")
        send_text(phone, _MSG_TRYON_FAILED)
        _update_session(supabase, session_id, {"state": "WAITING_CUSTOMER_PHOTO"})
        return

    update_payload = {"state": "DELIVERED", "active_generation_id": tryon_generation_id}

    if is_direct:
        new_balance = (balance_before or 0) - settings.CREDITS_PER_IMAGE
        supabase.table("credit_ledger").insert(
            {
                "shop_id": shop_id,
                "delta": -settings.CREDITS_PER_IMAGE,
                "reason": "whatsapp_direct_tryon",
                "balance_after": new_balance,
            }
        ).execute()
        update_payload["free_generations_used"] = int(session.get("free_generations_used") or 0) + 1

    _update_session(supabase, session_id, update_payload)


def _handle_buy_command(supabase, session: dict) -> None:
    phone = session["phone_number"]

    try:
        link = create_payment_link_for_shop(supabase, session["shop_id"], _BUY_PACK_ID)
    except Exception as exc:
        print(
            f"[whatsapp_state] failed to create payment link shop_id={session['shop_id']} "
            f"error={exc}"
        )
        send_text(phone, _MSG_BUY_FAILED)
        return

    send_text(
        phone,
        _MSG_BUY_OFFER_TEMPLATE.format(
            images=_BUY_PACK_IMAGES,
            rupees=_BUY_PACK_RUPEES,
            short_url=link["short_url"],
        ),
    )


def _handle_team_command(supabase, session: dict) -> None:
    phone = session["phone_number"]
    code = get_or_create_join_code(supabase, session["shop_id"])
    send_text(phone, _MSG_TEAM_INVITE_TEMPLATE.format(code=code))
    _update_session(supabase, session["id"], {})


def _handle_join_command(supabase, session: dict, code: str) -> None:
    phone = session["phone_number"]

    result = (
        supabase.table("shops")
        .select("id, name")
        .eq("whatsapp_join_code", code)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        send_text(phone, _MSG_JOIN_NOT_FOUND)
        _update_session(supabase, session["id"], {})
        return

    shop_id = str(rows[0]["id"])
    shop_name = rows[0].get("name") or "is"

    if shop_id == str(session.get("shop_id")):
        send_text(phone, _MSG_JOIN_ALREADY_MEMBER)
        _send_garment_menu(supabase, session)
        return

    folder_id = _get_or_create_whatsapp_folder(supabase, shop_id)

    send_text(phone, _MSG_JOIN_SUCCESS_TEMPLATE.format(shop_name=shop_name))
    _send_garment_menu(
        supabase,
        session,
        extra_fields={
            "shop_id": shop_id,
            "folder_id": folder_id,
            "fabric_image_id": None,
            "hero_image_id": None,
            "active_generation_id": None,
        },
    )


def _resolve_latest_done_generation_id(supabase, shop_id: str, session: dict) -> Optional[str]:
    candidate = session.get("active_generation_id")
    if candidate:
        candidate_result = (
            supabase.table("generations")
            .select("id, status")
            .eq("id", candidate)
            .eq("shop_id", shop_id)
            .limit(1)
            .execute()
        )
        candidate_rows = getattr(candidate_result, "data", None) or []
        if candidate_rows and candidate_rows[0].get("status") == "done":
            return str(candidate_rows[0]["id"])

    result = (
        supabase.table("generations")
        .select("id")
        .eq("shop_id", shop_id)
        .eq("status", "done")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if rows:
        return str(rows[0]["id"])
    return None


def _handle_tv_command(supabase, session: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]

    generation_id = _resolve_latest_done_generation_id(supabase, shop_id, session)
    if not generation_id:
        send_text(phone, _MSG_TV_NONE)
        return

    supabase.table("generations").update({"show_on_screen": True}).eq(
        "id", generation_id
    ).eq("shop_id", shop_id).execute()

    supabase.table("shop_screen_state").upsert(
        {
            "shop_id": shop_id,
            "live_generation_id": generation_id,
            "updated_at": _utc_now_iso(),
        }
    ).execute()

    send_text(phone, _MSG_TV_PUSHED)


def _handle_next_command(supabase, session: dict) -> None:
    phone = session["phone_number"]
    shop_id = session["shop_id"]

    supabase.table("shop_screen_state").upsert(
        {
            "shop_id": shop_id,
            "live_generation_id": None,
            "updated_at": _utc_now_iso(),
        }
    ).execute()

    send_text(phone, _MSG_TV_CLEARED)


def _dispatch(supabase, session: dict, msg: dict) -> None:
    phone = session["phone_number"]
    session_id = session["id"]
    kind = msg.get("kind")
    text = (msg.get("text") or "").strip()
    state = session.get("state") or "NEW"

    if kind == "text" and text.lower() in _RESET_COMMANDS:
        _reset_to_choosing_garment(supabase, session, reply=_MSG_RESET)
        return

    if kind == "text" and text.lower() == "buy":
        _handle_buy_command(supabase, session)
        return

    if kind == "text" and text.lower() == "team":
        _handle_team_command(supabase, session)
        return

    if kind == "text" and text.lower() == "tv":
        _handle_tv_command(supabase, session)
        return

    if kind == "text" and text.lower() == "next":
        _handle_next_command(supabase, session)
        return

    if kind == "text":
        join_match = _JOIN_COMMAND_RE.match(text.strip())
        if join_match:
            code = join_match.group(1).replace(" ", "").upper()
            _handle_join_command(supabase, session, code)
            return

    if state == "NEW":
        name = session.get("profile_name") or msg.get("profile_name") or "dost"
        send_text(phone, _MSG_GREETING.format(name=name))
        _send_garment_menu(supabase, session)
        return

    if state == "CHOOSING_GARMENT":
        normalized = text.lower()

        if kind == "text" and normalized == "custom":
            if session.get("enhanced_enabled"):
                send_text(phone, _MSG_ENHANCED_UNLOCKED)
                _update_session(supabase, session_id, {"state": "WAITING_GARMENT"})
            else:
                send_text(phone, _MSG_ENHANCED_LOCKED)
                _update_session(supabase, session_id, {})
            return

        templates, _is_own = _get_menu_templates(supabase, session)
        template = None

        if kind == "interactive":
            reply_id = msg.get("reply_id")
            template = next((t for t in templates if t["folder_id"] == reply_id), None)
        elif kind == "text" and normalized.isdigit():
            idx = int(normalized)
            if 1 <= idx <= len(templates):
                template = templates[idx - 1]

        if template is None:
            send_text(phone, _MSG_GARMENT_MENU_INVALID)
            _update_session(supabase, session_id, {})
            return

        folder_override: Optional[dict] = None

        if template.get("is_own"):
            # Own garment — hero image already lives in this shop, and slots
            # / prompt context must come from the own garment's folder, not
            # the generic WhatsApp container folder.
            hero_image_id = template["hero_image_id"]
            session["folder_id"] = template["folder_id"]
            folder_override = {"folder_id": template["folder_id"]}
        else:
            try:
                hero_image_id = _resolve_shadow_hero_image(
                    supabase,
                    shop_id=session["shop_id"],
                    folder_id=session["folder_id"],
                    template=template,
                )
            except Exception as exc:
                print(f"[whatsapp_state] failed to resolve master hero image: {exc}")
                send_text(phone, _MSG_TECH_ERROR)
                _update_session(supabase, session_id, {})
                return

        fabric_slots = _load_fabric_slots(supabase, session["folder_id"]) if template.get("is_own") else []

        if template.get("is_own") and fabric_slots:
            settings = get_settings()
            if _get_shop_balance(supabase, session["shop_id"]) < settings.CREDITS_PER_IMAGE:
                send_text(phone, _MSG_NO_CREDITS)
                _update_session(supabase, session_id, {})
                return

            collecting_payload = {
                "state": "COLLECTING_FABRICS",
                "hero_image_id": hero_image_id,
                "pending_fabrics": [],
            }
            if folder_override:
                collecting_payload.update(folder_override)
            _update_session(supabase, session_id, collecting_payload)
            send_text(
                phone,
                _MSG_MULTIFABRIC_PROMPT.format(n=1, total=len(fabric_slots), label=fabric_slots[0]["label"]),
            )
            return

        waiting_payload = {"state": "WAITING_FABRIC", "hero_image_id": hero_image_id}
        if folder_override:
            waiting_payload.update(folder_override)
        _update_session(supabase, session_id, waiting_payload)
        send_text(phone, _MSG_GARMENT_CHOSEN_TEMPLATE.format(garment=template["name"]))
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

    if state == "COLLECTING_FABRICS":
        _handle_collecting_fabrics(supabase, session, msg)
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
        elif kind == "text" and normalized == "menu":
            _send_garment_menu(supabase, session)
        elif kind == "image":
            _handle_fabric_image(supabase, session, msg, mode_menu_suffix=_MSG_MENU_HINT_SUFFIX)
        else:
            send_text(phone, _MSG_OFFER_TRYON_REPEAT)
            _update_session(supabase, session_id, {})
        return

    if state == "PROCESSING":
        send_text(phone, _MSG_PROCESSING)
        _update_session(supabase, session_id, {})
        return

    if state == "DELIVERED":
        normalized = text.lower()
        if kind == "text" and normalized == "menu":
            _send_garment_menu(supabase, session)
        elif kind == "image":
            _handle_fabric_image(supabase, session, msg, mode_menu_suffix=_MSG_MENU_HINT_SUFFIX)
        elif kind == "text":
            send_text(phone, _MSG_DELIVERED_TEXT)
            _update_session(supabase, session_id, {})
        else:
            send_text(phone, _MSG_ONLY_PHOTO)
            _update_session(supabase, session_id, {})
        return

    if state == "ERROR":
        _reset_to_choosing_garment(supabase, session, reply=_MSG_ERROR_RECOVERY)
        return

    # Unknown/legacy state: recover into a known-good state.
    _reset_to_choosing_garment(supabase, session, reply=_MSG_ERROR_RECOVERY)


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
