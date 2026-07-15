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
                    reply_id = None

                    if msg_type == "text":
                        kind = "text"
                        text = (message.get("text") or {}).get("body")
                    elif msg_type == "image":
                        kind = "image"
                        image = message.get("image") or {}
                        media_id = image.get("id")
                        mime_type = image.get("mime_type")
                    elif msg_type == "interactive":
                        kind = "interactive"
                        interactive = message.get("interactive") or {}
                        list_reply = interactive.get("list_reply") or {}
                        button_reply = interactive.get("button_reply") or {}
                        reply_id = list_reply.get("id") or button_reply.get("id")
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
                            "reply_id": reply_id,
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


def _build_numbered_menu_text(body: str, rows: list[dict]) -> str:
    lines = [body, ""]
    for idx, row in enumerate(rows, start=1):
        lines.append(f"{idx}. {row.get('title', '')}")
    lines.append("")
    lines.append("Reply with a number.")
    return "\n".join(lines)


def send_interactive_list(
    to_phone: str,
    header: str,
    body: str,
    button_label: str,
    rows: list[dict],
) -> None:
    """Send a WhatsApp interactive list message (max 10 rows). On any
    failure (non-200 response or transport exception), falls back to a
    plain numbered-menu text message built from the same rows, so the
    flow never dead-ends on clients/accounts that reject interactive
    messages."""
    settings = get_settings()
    url = f"{GRAPH_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"

    capped_rows = rows[:10]
    list_rows = [
        {"id": str(row["id"]), "title": str(row["title"])[:24]} for row in capped_rows
    ]

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "action": {
                "button": button_label,
                "sections": [{"title": header, "rows": list_rows}],
            },
        },
    }

    try:
        response = _client.post(url, headers=_auth_headers(), json=payload)
        if response.status_code != 200:
            print(
                f"[whatsapp_transport] send_interactive_list failed to={to_phone} "
                f"status={response.status_code} body={response.text}"
            )
            send_text(to_phone, _build_numbered_menu_text(body, capped_rows))
    except Exception as exc:
        print(f"[whatsapp_transport] send_interactive_list raised to={to_phone} error={exc}")
        send_text(to_phone, _build_numbered_menu_text(body, capped_rows))


def upload_media(image_bytes: bytes, mime_type: str) -> str:
    settings = get_settings()
    url = f"{GRAPH_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/media"
    data = {"messaging_product": "whatsapp", "type": mime_type}
    files = {"file": ("upload", image_bytes, mime_type)}

    try:
        response = _client.post(url, headers=_auth_headers(), data=data, files=files)
    except Exception as exc:
        raise WhatsAppTransportError(f"upload_media raised: {exc}")

    if response.status_code != 200:
        raise WhatsAppTransportError(
            f"upload_media failed status={response.status_code} body={response.text}"
        )

    media_id = response.json().get("id")
    if not media_id:
        raise WhatsAppTransportError("upload_media response missing media id")

    return media_id


def send_image_by_media_id(to_phone: str, media_id: str, caption: str = "") -> None:
    settings = get_settings()
    url = f"{GRAPH_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "image",
        "image": {"id": media_id, "caption": caption},
    }

    try:
        response = _client.post(url, headers=_auth_headers(), json=payload)
    except Exception as exc:
        raise WhatsAppTransportError(f"send_image_by_media_id raised: {exc}")

    if response.status_code != 200:
        raise WhatsAppTransportError(
            f"send_image_by_media_id failed status={response.status_code} body={response.text}"
        )


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
