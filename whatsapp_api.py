from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import PlainTextResponse

from config import get_settings
from whatsapp_transport import parse_webhook_payload, send_text

router = APIRouter(tags=["WhatsApp"])

_UNDER_CONSTRUCTION_REPLY = (
    "MyTryonAi bot is under construction. Aap jald hi yahan apne kapde ka "
    "AI look bana payenge!"
)


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

        if kind not in ("text", "image"):
            continue

        try:
            print(
                f"[whatsapp] from={event.get('from_phone')} kind={kind} "
                f"text={event.get('text')} media={event.get('media_id')}"
            )

            if kind == "text":
                send_text(event.get("from_phone"), _UNDER_CONSTRUCTION_REPLY)
        except Exception as exc:
            print(f"[whatsapp] failed to process event error={exc}")
