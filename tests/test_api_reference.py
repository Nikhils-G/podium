"""The API reference page is rendered from the OpenAPI document, so it cannot drift from it."""

import re

from podium.services.webhooks import EVENT_TYPES


def _op_id(method: str, path: str) -> str:
    return "op-" + re.sub(r"[^a-z0-9]+", "-", f"{method}-{path}").strip("-")


def test_reference_lists_every_operation_without_javascript(client):
    spec = client.get("/api/openapi.json").json()
    operations = [(method, path) for path, item in spec["paths"].items() for method in item]
    page = client.get("/api/docs")
    assert page.status_code == 200
    html = page.text
    for method, path in operations:
        assert f'id="{_op_id(method, path)}"' in html, (method, path)
    assert f"{len(operations)} of {len(operations)} endpoints" in html
    assert "swagger" not in html.lower()
    for event_type in EVENT_TYPES:
        assert event_type in html
    assert "unsafe-inline" not in page.headers["content-security-policy"]


def test_reference_states_permissions_limits_and_examples(client):
    html = client.get("/api/docs").text
    assert "Event organizer" in html
    assert "Judge of the event (organizers too)" in html
    assert "Team member, while submissions are open" in html
    assert "30 requests per minute per IP address" in html
    assert "Authorization: Bearer $PODIUM_TOKEN" in html
    assert "-d &#39;{&#34;title&#34;: &#34;Quiet Hours&#34;" in html  # ProjectCreate example body
    assert "curl http://localhost:8080/api/v1/events/sample-hack-2026/projects/prj_07" in html


def test_reference_filters_server_side(client):
    html = client.get("/api/docs?q=certificate").text
    assert f'id="{_op_id("get", "/api/v1/verify/{serial}")}"' in html
    assert f'id="{_op_id("get", "/api/v1/events/{slug}/tally")}"' not in html
    assert 'id="overview"' not in html  # the guide steps aside while filtering
    empty = client.get("/api/docs?q=zzz-nothing-matches").text
    assert "No endpoints match" in empty and "Show all endpoints" in empty


def test_interactive_console_survives_a_boosted_visit(client):
    page = client.get("/api/docs/console")
    assert page.status_code == 200
    assert "/static/vendor/swagger-ui/swagger-ui-bundle.js" in page.text
    assert "DOMContentLoaded" not in page.text  # must start even when htmx swapped the body in
    assert "unsafe-inline" in page.headers["content-security-policy"]
    assert "cdn" not in page.text.lower()


def test_validation_errors_share_the_error_shape(client, auth):
    r = client.post("/api/v1/events", json={}, headers=auth("organizer"))
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_failed" and body["errors"]["name"]
    spec = client.get("/api/openapi.json").json()
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert (
        spec["paths"]["/api/v1/events"]["post"]["responses"]["422"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )


def _article(html: str, key: str) -> str:
    start = html.index(f'id="{key}"')
    return html[start : html.index("</article>", start)]


def test_every_endpoint_has_three_request_samples_and_a_response_panel(client):
    html = client.get("/api/docs").text
    total = int(re.search(r"(\d+) of \d+ endpoints", html).group(1))
    assert html.count('class="api-panel__radio"') == 3 * total
    events = _article(html, "op-get-api-v1-events")
    assert "requests.get(" in events and "await fetch(" in events
    assert "&#34;events&#34;: [" in events  # the typed response example, in its Copy button
    tracks = _article(html, "op-post-api-v1-events-slug-tracks")
    assert "Returns a JSON object" in tracks
    delete = _article(html, "op-delete-api-v1-events-slug-tracks-track-id")
    assert "No body. The status code is the answer." in delete
    csv = _article(html, "op-get-api-v1-events-slug-exports-name-csv")
    assert "text/csv" in csv and "print(response.text)" in csv
    samples = re.findall(r'data-lang="(?:curl|python|javascript)" data-copy="([^"]*)"', html)
    assert len(samples) == 3 * total
    assert not [text for text in samples if re.search(r"\{[a-z_]+\}", text)]  # no unfilled path


def test_endpoint_index_rows_point_at_real_cards(client):
    html = client.get("/api/docs").text
    keys = re.findall(r'data-api-index-for="([^"]+)"', html)
    assert keys and all(f'id="{key}"' in html for key in keys)


def test_code_is_highlighted_on_the_server(client):
    html = client.get("/api/docs").text
    assert '<span class="tok-p">&#34;events&#34;</span>' in html
    assert '<span class="tok-f">-H</span>' in html
    assert "<script" not in html.split("</head>")[1].split('src="/static/js/app.js"')[0]
