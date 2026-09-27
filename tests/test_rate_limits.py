"""Rate limits key on the socket peer, so a client-sent X-Forwarded-For cannot dodge them.
conftest turns limiting off; these tests switch it on for themselves and always reset."""

from starlette.requests import Request

from podium.config import get_settings
from podium.security import ratelimit
from tests.conftest import SLUG


def test_client_ip_ignores_a_client_sent_forwarded_header():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-for", b"203.0.113.99")],
        "client": ("198.51.100.7", 5555),
    }
    assert ratelimit.client_ip(Request(scope)) == "198.51.100.7"


def test_api_code_redeem_is_limited_even_with_rotating_forwarded_headers(app, client):
    limited = get_settings().model_copy(update={"rate_limit_enabled": True})
    app.dependency_overrides[get_settings] = lambda: limited
    ratelimit.reset()
    try:
        url = f"/api/v1/events/{SLUG}/voting/codes/redeem"
        statuses = [
            client.post(
                url, json={"code": "AAAA-AAAA"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}
            ).status_code
            for i in range(11)
        ]
        # a bogus code is refused with 404 and writes nothing; the 11th try hits the limit
        assert statuses == [404] * 10 + [429]
    finally:
        app.dependency_overrides.pop(get_settings, None)
        ratelimit.reset()
