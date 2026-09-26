"""Outbound webhooks. Emitting creates a persisted delivery row per subscribed hook; a worker
POSTs it with an HMAC signature and retries 30 s → 5 min → 30 min before marking it failed."""

import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from podium.errors import NotFound, ValidationFailed
from podium.models import DeliveryStatus, Event, User, Webhook, WebhookDelivery, utcnow
from podium.services import audit

EVENT_TYPES = [
    "project.submitted",
    "project.withdrawn",
    "review.submitted",
    "assignment.created",
    "judging.opened",
    "judging.closed",
    "results.published",
    "results.unpublished",
    "certificate.issued",
]
BACKOFF = [timedelta(seconds=30), timedelta(minutes=5), timedelta(minutes=30)]
TIMEOUT = 5


def list_hooks(db: DbSession, event: Event) -> list[Webhook]:
    return list(
        db.execute(
            select(Webhook).where(Webhook.event_id == event.id).order_by(Webhook.id)
        ).scalars()
    )


def create_hook(
    db: DbSession, event: Event, user: User, url: str, event_types: list[str], secret: str = ""
) -> Webhook:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ValidationFailed(
            errors={"url": "Enter a full URL starting with http:// or https://."}
        )
    types = [t for t in event_types if t in EVENT_TYPES] or EVENT_TYPES[:]
    hook = Webhook(
        event_id=event.id,
        url=url,
        secret=secret.strip() or secrets.token_urlsafe(24),
        event_types=types,
    )
    db.add(hook)
    db.flush()
    audit.record(
        db,
        "webhook.created",
        "webhook",
        hook.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"url": url, "events": types},
    )
    db.commit()
    return hook


def get_hook(db: DbSession, event: Event, public_id: str) -> Webhook:
    hook = db.execute(
        select(Webhook).where(Webhook.event_id == event.id, Webhook.public_id == public_id)
    ).scalar_one_or_none()
    if hook is None:
        raise NotFound("No such webhook.")
    return hook


def set_active(db: DbSession, event: Event, user: User, hook: Webhook, active: bool) -> Webhook:
    hook.active = active
    audit.record(
        db,
        "webhook.enabled" if active else "webhook.disabled",
        "webhook",
        hook.public_id,
        event_id=event.id,
        actor_id=user.id,
    )
    db.commit()
    return hook


def delete_hook(db: DbSession, event: Event, user: User, hook: Webhook) -> None:
    audit.record(
        db,
        "webhook.deleted",
        "webhook",
        hook.public_id,
        event_id=event.id,
        actor_id=user.id,
        meta={"url": hook.url},
    )
    db.delete(hook)
    db.commit()


def emit(db: DbSession, event: Event, event_type: str, payload: dict) -> int:
    """Queue a delivery for every active hook subscribed to this type. Caller commits."""
    hooks = (
        db.execute(select(Webhook).where(Webhook.event_id == event.id, Webhook.active.is_(True)))
        .scalars()
        .all()
    )
    body = {"type": event_type, "event": event.slug, "at": utcnow().isoformat(), "data": payload}
    n = 0
    for hook in hooks:
        if event_type in hook.event_types:
            db.add(
                WebhookDelivery(
                    webhook_id=hook.id,
                    event_type=event_type,
                    payload=body,
                    next_attempt_at=utcnow(),
                )
            )
            n += 1
    return n


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def http_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}"
    except Exception as exc:  # network errors, timeouts, bad URLs
        return 0, f"{type(exc).__name__}: {exc}"


Sender = Callable[[str, bytes, dict[str, str]], tuple[int, str]]


def deliver_pending(db: DbSession, *, sender: Sender = http_post, limit: int = 20) -> int:
    """Attempt every due delivery once. Returns how many were attempted."""
    due = (
        db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == DeliveryStatus.pending,
                WebhookDelivery.next_attempt_at <= utcnow(),
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    for delivery in due:
        hook = delivery.webhook
        body = json.dumps(delivery.payload, sort_keys=True, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Podium-Webhooks/1.0",
            "X-Podium-Event": delivery.event_type,
            "X-Podium-Delivery": str(delivery.id),
            "X-Podium-Signature": sign(hook.secret, body),
        }
        status, error = sender(hook.url, body, headers)
        delivery.attempts += 1
        delivery.last_status_code = status or None
        if 200 <= status < 300:
            delivery.status = DeliveryStatus.delivered
            delivery.delivered_at = utcnow()
            delivery.last_error = None
            delivery.next_attempt_at = None
        else:
            delivery.last_error = error or f"HTTP {status}"
            if delivery.attempts > len(BACKOFF):
                delivery.status = DeliveryStatus.failed
                delivery.next_attempt_at = None
            else:
                delivery.next_attempt_at = utcnow() + BACKOFF[delivery.attempts - 1]
    db.commit()
    return len(due)


def redeliver(db: DbSession, event: Event, user: User, delivery_id: int) -> WebhookDelivery:
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.webhook.event_id != event.id:
        raise NotFound("No such delivery.")
    delivery.status = DeliveryStatus.pending
    delivery.attempts = 0
    delivery.next_attempt_at = utcnow()
    audit.record(
        db,
        "webhook.redelivered",
        "webhook_delivery",
        delivery.id,
        event_id=event.id,
        actor_id=user.id,
    )
    db.commit()
    return delivery


def recent_deliveries(db: DbSession, event: Event, limit: int = 50) -> list[WebhookDelivery]:
    return list(
        db.execute(
            select(WebhookDelivery)
            .join(Webhook, Webhook.id == WebhookDelivery.webhook_id)
            .where(Webhook.event_id == event.id)
            .order_by(WebhookDelivery.id.desc())
            .limit(limit)
        ).scalars()
    )
