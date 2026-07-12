import httpx

from config import get_settings

_settings = get_settings()
GRAPH_BASE = f"https://graph.facebook.com/{_settings.WHATSAPP_GRAPH_VERSION}"

_client = httpx.Client(timeout=30)


class WhatsAppTransportError(Exception):
    pass


def _auth_headers() -> dict:
    settings = get_settings()
    return {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}


def parse_webhook_payload(body: dict) -> list[dict]:
    events: list[dict] = []

    try:
        entries = body.get("entry") or []
        for entry in entries:
            changes = entry.get("changes") or []
            for change in changes:
                value = change.get("value") or {}

                if value.get("statuses"):
                    events.append({"kind": "status"})

                contacts = value.get("contacts") or []
                profile_name = None
                if contacts:
                    profile_name = (contacts[0].get("profile") or {}).get("name")

                messages = value.get("messages") or []
                for message in messages:
                    msg_type = message.get("type")

                    text = None
                    media_id = None
                    mime_type = None

                    if msg_type == "text":
                        kind = "text"
                        text = (message.get("text") or {}).get("body")
                    elif msg_type == "image":
                        kind = "image"
                        image = message.get("image") or {}
                        media_id = image.get("id")
                        mime_type = image.get("mime_type")
                    else:
                        kind = "other"

                    events.append(
                        {
                            "kind": kind,
                            "from_phone": message.get("from"),
                            "profile_name": profile_name,
                            "message_id": message.get("id"),
                            "text": text,
                            "media_id": media_id,
                            "mime_type": mime_type,
                        }
                    )
    except Exception:
        return []

    return events


def download_media(media_id: str) -> tuple[bytes, str]:
    meta_response = _client.get(f"{GRAPH_BASE}/{media_id}", headers=_auth_headers())
    if meta_response.status_code != 200:
        raise WhatsAppTransportError(
            f"Failed to fetch media metadata for {media_id}: "
            f"{meta_response.status_code} {meta_response.text}"
        )

    meta = meta_response.json()
    media_url = meta.get("url")
    mime_type = meta.get("mime_type")

    content_response = _client.get(media_url, headers=_auth_headers())
    if content_response.status_code != 200:
        raise WhatsAppTransportError(
            f"Failed to download media {media_id}: "
            f"{content_response.status_code} {content_response.text}"
        )

    return content_response.content, mime_type


def send_text(to_phone: str, body: str) -> None:
    settings = get_settings()
    url = f"{GRAPH_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": body},
    }

    try:
        response = _client.post(url, headers=_auth_headers(), json=payload)
        if response.status_code != 200:
            print(
                f"[whatsapp_transport] send_text failed to={to_phone} "
                f"status={response.status_code} body={response.text}"
            )
    except Exception as exc:
        print(f"[whatsapp_transport] send_text raised to={to_phone} error={exc}")


def send_image_by_link(to_phone: str, image_url: str, caption: str = "") -> None:
    settings = get_settings()
    url = f"{GRAPH_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }

    try:
        response = _client.post(url, headers=_auth_headers(), json=payload)
        if response.status_code != 200:
            print(
                f"[whatsapp_transport] send_image_by_link failed to={to_phone} "
                f"status={response.status_code} body={response.text}"
            )
    except Exception as exc:
        print(f"[whatsapp_transport] send_image_by_link raised to={to_phone} error={exc}")
