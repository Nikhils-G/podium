from pydantic import BaseModel, Field


class WebhookCreate(BaseModel):
    url: str = Field(..., max_length=500)
    events: list[str] = Field(
        default_factory=list, description="Subset of the event types; empty = all"
    )
    secret: str = Field("", max_length=128, description="Optional; generated when empty")


class WebhookUpdate(BaseModel):
    active: bool


class TokenCreate(BaseModel):
    name: str = Field(..., max_length=80)


def webhook_out(h) -> dict:
    return {
        "id": h.public_id,
        "url": h.url,
        "events": h.event_types,
        "active": h.active,
        "created_at": h.created_at,
    }


def delivery_out(d) -> dict:
    return {
        "id": d.id,
        "webhook": d.webhook.public_id,
        "type": d.event_type,
        "status": d.status.value,
        "attempts": d.attempts,
        "last_status_code": d.last_status_code,
        "last_error": d.last_error,
        "next_attempt_at": d.next_attempt_at,
        "delivered_at": d.delivered_at,
        "created_at": d.created_at,
    }


def certificate_out(c) -> dict:
    return {
        "serial": c.serial,
        "kind": c.kind.value,
        "payload": c.payload,
        "signature": c.signature,
        "issued_at": c.issued_at,
        "revoked_at": c.revoked_at,
    }
