import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

import razorpay
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from admin_api import _append_credit_ledger
from auth_deps import CurrentShopContext, get_current_shop_context
from config import get_settings
from supabase_client import get_supabase_admin_client
from whatsapp_transport import send_text

router = APIRouter(tags=["Payments"])

# Single source of truth for purchasable credit packs. Amounts are in paise
# (Razorpay's smallest currency unit for INR): Rs.70 = 7000 paise. Packs are
# authored in images; credits = images * CREDITS_PER_IMAGE so the ledger
# stores the perception-priced amount while the pack definition stays in the
# unit tailors actually think in. Adding a new pack later is a one-line
# addition here.
_CREDITS_PER_IMAGE = get_settings().CREDITS_PER_IMAGE

CREDIT_PACKS: dict[str, dict[str, Any]] = {
    "starter": {
        "images": 5,
        "credits": 5 * _CREDITS_PER_IMAGE,
        "amount_paise": 7000,
        "label": "5 looks - Rs.70",
    },
}

_WEBHOOK_EVENTS = {"payment_link.paid", "order.paid", "payment.captured"}


@lru_cache
def _get_razorpay_client() -> razorpay.Client:
    settings = get_settings()
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _get_pack_or_400(pack_id: str) -> dict[str, Any]:
    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid pack_id")
    return pack


class CreateOrderRequest(BaseModel):
    pack_id: str = Field(..., min_length=1)


@router.get("/payments/packs")
def get_credit_packs():
    return CREDIT_PACKS


@router.post("/payments/create-order")
def create_order(
    body: CreateOrderRequest,
    current: CurrentShopContext = Depends(get_current_shop_context),
):
    settings = get_settings()
    pack = _get_pack_or_400(body.pack_id)
    client = _get_razorpay_client()

    try:
        order = client.order.create(
            {
                "amount": pack["amount_paise"],
                "currency": "INR",
                "notes": {
                    "shop_id": current.shop_id,
                    "pack_id": body.pack_id,
                    "source": "react",
                },
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create Razorpay order",
        ) from exc

    supabase = get_supabase_admin_client()
    try:
        supabase.table("razorpay_payments").insert(
            {
                "shop_id": current.shop_id,
                "pack_id": body.pack_id,
                "credits": pack["credits"],
                "amount_paise": pack["amount_paise"],
                "source": "react",
                "razorpay_order_id": order["id"],
                "status": "created",
            }
        ).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Order created in Razorpay but failed to record locally",
        ) from exc

    return {
        "order_id": order["id"],
        "amount_paise": pack["amount_paise"],
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "pack": {"pack_id": body.pack_id, **pack},
    }


def create_payment_link_for_shop(supabase, shop_id: str, pack_id: str) -> dict[str, str]:
    """Create a Razorpay Payment Link for a shop's credit top-up.

    Called directly by the WhatsApp bot flow (no internal HTTP hop).
    """
    pack = _get_pack_or_400(pack_id)
    client = _get_razorpay_client()

    try:
        link = client.payment_link.create(
            {
                "amount": pack["amount_paise"],
                "currency": "INR",
                "description": f"MyTryonAi {pack['label']}",
                "notes": {
                    "shop_id": shop_id,
                    "pack_id": pack_id,
                    "source": "whatsapp",
                },
                "reminder_enable": True,
            }
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to create Razorpay payment link: {exc}") from exc

    supabase.table("razorpay_payments").insert(
        {
            "shop_id": shop_id,
            "pack_id": pack_id,
            "credits": pack["credits"],
            "amount_paise": pack["amount_paise"],
            "source": "whatsapp",
            "razorpay_payment_link_id": link["id"],
            "status": "created",
        }
    ).execute()

    return {"short_url": link["short_url"], "payment_link_id": link["id"]}


def _find_payment_row(
    supabase,
    *,
    razorpay_payment_id: Optional[str],
    razorpay_order_id: Optional[str],
    razorpay_payment_link_id: Optional[str],
) -> Optional[dict[str, Any]]:
    # Retry of a webhook we've already fully processed: matched by the
    # payment id we stamped onto the row the first time we credited it.
    if razorpay_payment_id:
        result = (
            supabase.table("razorpay_payments")
            .select("*")
            .eq("razorpay_payment_id", razorpay_payment_id)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if rows:
            return rows[0]

    # First webhook for this payment: locate the "created" row we inserted
    # at order/payment-link creation time.
    if razorpay_order_id:
        result = (
            supabase.table("razorpay_payments")
            .select("*")
            .eq("razorpay_order_id", razorpay_order_id)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if rows:
            return rows[0]

    if razorpay_payment_link_id:
        result = (
            supabase.table("razorpay_payments")
            .select("*")
            .eq("razorpay_payment_link_id", razorpay_payment_link_id)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        if rows:
            return rows[0]

    return None


def _extract_notes(*entities: dict[str, Any]) -> dict[str, Any]:
    for entity in entities:
        notes = entity.get("notes")
        if isinstance(notes, dict) and notes:
            return notes
    return {}


def _notify_whatsapp_topup(shop_id: str, credits: int) -> None:
    try:
        supabase = get_supabase_admin_client()
        result = (
            supabase.table("whatsapp_sessions")
            .select("phone_number")
            .eq("shop_id", shop_id)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        phones = sorted({str(row["phone_number"]) for row in rows if row.get("phone_number")})

        images = credits // get_settings().CREDITS_PER_IMAGE
        message = (
            f"Payment mil gaya! {images} looks add ho gaye \U0001F389 "
            "Ab agla FABRIC bhejiye \U0001F4F8"
        )
        for phone in phones:
            send_text(phone, message)
    except Exception as exc:
        print(f"[payments] whatsapp top-up notification failed shop_id={shop_id} error={exc}")


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request, background_tasks: BackgroundTasks):
    settings = get_settings()

    # Signature verification requires the exact raw bytes Razorpay signed -
    # re-serializing a parsed body would produce a different byte string
    # and always fail verification.
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    client = _get_razorpay_client()
    try:
        client.utility.verify_webhook_signature(
            raw.decode("utf-8"), signature, settings.RAZORPAY_WEBHOOK_SECRET
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        ) from exc

    body = json.loads(raw.decode("utf-8"))
    event = body.get("event") or ""
    if event not in _WEBHOOK_EVENTS:
        return {"status": "ignored", "event": event}

    payload = body.get("payload") or {}
    payment_entity = ((payload.get("payment") or {}).get("entity")) or {}
    order_entity = ((payload.get("order") or {}).get("entity")) or {}
    payment_link_entity = ((payload.get("payment_link") or {}).get("entity")) or {}

    razorpay_payment_id = payment_entity.get("id")
    razorpay_order_id = order_entity.get("id") or payment_entity.get("order_id")
    razorpay_payment_link_id = payment_link_entity.get("id")

    # Notes set at order/payment-link creation are normally copied onto the
    # resulting payment entity; fall back to the order/link entity notes in
    # case a given event payload doesn't carry them on the payment itself.
    notes = _extract_notes(payment_entity, order_entity, payment_link_entity)

    supabase = get_supabase_admin_client()
    row = _find_payment_row(
        supabase,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_link_id=razorpay_payment_link_id,
    )

    if row is None:
        print(
            f"[payments] webhook event={event} payment_id={razorpay_payment_id} "
            "matched no razorpay_payments row"
        )
        return {"status": "ignored"}

    if row.get("status") == "credited":
        return {"status": "ok", "already_credited": True}

    shop_id = str(row["shop_id"])
    pack_id = str(row["pack_id"])
    credits = int(row["credits"])
    source = row.get("source") or notes.get("source")

    _append_credit_ledger(supabase, shop_id, delta=credits, reason=f"razorpay_{pack_id}")

    (
        supabase.table("razorpay_payments")
        .update(
            {
                "status": "credited",
                "razorpay_payment_id": razorpay_payment_id,
                "credited_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", row["id"])
        .execute()
    )

    if source == "whatsapp":
        background_tasks.add_task(_notify_whatsapp_topup, shop_id, credits)

    return {"status": "ok"}
