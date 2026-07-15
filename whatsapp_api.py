from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import PlainTextResponse

from config import get_settings
from whatsapp_state import handle_incoming
from whatsapp_transport import parse_webhook_payload

router = APIRouter(tags=["WhatsApp"])


@router.get("/webhook/whatsapp")
def verify_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
):
    settings = get_settings()

    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)

    return PlainTextResponse(content="Forbidden", status_code=403)


@router.post("/webhook/whatsapp")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    background_tasks.add_task(process_webhook_events, body)
    return {"status": "ok"}


def process_webhook_events(body: dict) -> None:
    try:
        events = parse_webhook_payload(body)
    except Exception as exc:
        print(f"[whatsapp] failed to parse webhook payload error={exc}")
        return

    for event in events:
        kind = event.get("kind")

        if kind not in ("text", "image", "interactive"):
            continue

        try:
            print(
                f"[whatsapp] from={event.get('from_phone')} kind={kind} "
                f"text={event.get('text')} media={event.get('media_id')}"
            )

            handle_incoming(event)
        except Exception as exc:
            print(f"[whatsapp] failed to process event error={exc}")
