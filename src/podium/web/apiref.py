"""Server-rendered API reference.

Turns the OpenAPI schema plus each route's declared dependencies into flat rows a template can
print: method, path, who may call it, parameters, body fields, responses, request samples in
three languages and a response example. Built once per process and cached on ``app.state``; the
page needs no JavaScript to be read.
"""

from __future__ import annotations

import json
import pprint
import re
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.routing import APIRoute
from markupsafe import Markup, escape

from podium.models.events import Role
from podium.security import deps

ROLE_LABELS = {
    Role.judge: "Judge",
    Role.participant: "Team member",
    Role.organizer: "Event organizer",
}
SAMPLE_VALUES = {
    "slug": "sample-hack-2026",
    "pid": "prj_07",
    "project_id": "prj_07",
    "judge_id": "jdg_24",
    "serial": "PDM-EVT01-4K7Q2M9Z",
    "code": "ABCD-EFGH",
    "name": "scores",
    "kind": "participation",
    "action": "open_judging",
    "track_id": "trk_01",
    "prize_id": "prz_01",
    "user_id": "usr_01",
    "criterion_id": "crt_01",
    "cid": "cmt_01",
    "hook_id": "whk_01",
    "delivery_id": "1",
    "token_id": "1",
    "vote_id": "1",
    "assignment_id": "1",
    "invite_id": "1",
}
_MISSING = object()
# Example values for generated bodies: (schema, field) wins over a field name alone. They mirror
# what the real endpoints return (team and track *names* on projects, ids elsewhere).
FIELD_SAMPLES: dict = {
    ("EventOut", "name"): "Sample Hack 2026",
    ("EventOut", "description"): "41 projects, 8 tracks, 30 judges.",
    ("TrackOut", "name"): "Developer tools",
    ("TrackOut", "description"): "Tools for developers.",
    ("TrackCreate", "name"): "Developer tools",
    ("PrizeOut", "name"): "Best overall",
    ("PrizeCreate", "name"): "Best overall",
    ("PrizeOut", "amount"): "$800",
    ("PrizeCreate", "amount"): "$800",
    ("PrizeOut", "project"): "prj_34",
    ("UserOut", "name"): "Olivia Organizer",
    ("UserOut", "email"): "olivia@example.org",
    ("CriterionCreate", "name"): "Functionality",
    ("TeamCreate", "name"): "Night Owls",
    ("TokenCreate", "name"): "CI export",
    ("TokenOut", "name"): "CI export",
    ("ProjectOut", "team"): "Night Owls",
    ("ProjectOut", "track"): "Accessibility",
    ("ResultRow", "project"): "prj_34",
    ("ResultRow", "title"): "Iron Switch",
    ("ResultRow", "track"): "Open hardware",
    ("ProjectOut", "status"): "submitted",
    ("VerifyOut", "status"): "valid",
    ("InviteCreate", "email"): "judge@example.org",
    ("ReviewWrite", "scores"): {"crt_01": 4, "crt_02": 5, "crt_03": 3},
    ("CertificateOut", "payload"): {
        "kind": "participation",
        "event": {"name": "Sample Hack 2026"},
        "recipient": {"name": "Ada Lovelace"},
    },
    "slug": "sample-hack-2026",
    "event": "sample-hack-2026",
    "title": "Quiet Hours",
    "summary": "A focus timer that mutes notifications for the whole team.",
    "description": "",
    "stage": "judging",
    "project": "prj_07",
    "judge": "jdg_24",
    "track": "trk_01",
    "serial": "PDM-EVT01-4K7Q2M9Z",
    "prefix": "pdm_4k7q",
    "scope": "read",
    "kind": "participation",
    "method": "zscore",
    "basis": "normalized",
    "code": "ABCD-EFGH",
    "reason": "Duplicate ballot from a shared laptop.",
    "body": "Lovely demo. Does it work offline?",
    "comment": "Clear problem statement; the demo crashed once.",
    "voting_mode": "account",
    "public_key_hex": "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29",
    "signature": "u8Tq0c9vX1…Qk2g==",
    "repo_url": "https://github.com/example/quiet-hours",
    "demo_url": "https://quiet-hours.example.org",
    "video_url": "",
    "url": "https://example.org/hooks/podium",
    "total": 41,
    "page": 1,
    "pages": 1,
    "reviews": 3,
    "votes": 12,
    "voters": 9,
    "total_votes": 120,
    "total_voters": 57,
    "voided": 2,
    "global_mean": 64.15,
    "global_std": 16.21,
    "raw_mean": 83.33,
    "normalized": 83.78,
    "rank": 1,
    "rank_raw": 1,
    "rank_normalized": 1,
    "disagreement": 0.18,
    "mean": 59.09,
    "std": 14.85,
    "shrunk_mean": 59.87,
    "shrunk_std": 15.05,
    "max_team_size": 4,
    "weight": 1.0,
    "min_score": 1,
    "max_score": 5,
    "count": 50,
    "reviews_per_project": 3,
    "seed": 1,
    "voting_credits": 5,
    "flat": False,
    "is_admin": False,
    "tied": False,
}
ID_SAMPLES = {
    "EventOut": "evt_01",
    "ProjectOut": "prj_07",
    "TrackOut": "trk_01",
    "PrizeOut": "prz_01",
    "UserOut": "usr_01",
    "TokenOut": 7,
}
DATE_SAMPLES = {
    "submissions_open_at": "2026-10-01T18:00:00Z",
    "submissions_close_at": "2026-10-03T18:00:00Z",
    "submitted_at": "2026-10-03T16:42:00Z",
    "issued_at": "2026-10-05T12:00:00Z",
    "created_at": "2026-09-28T09:15:00Z",
    "expires_at": "2026-12-27T09:15:00Z",
}
NULL_FIELDS = {  # optional timestamps that are still empty in a typical example
    "voting_open_at",
    "voting_close_at",
    "results_published_at",
    "archived_at",
    "revoked_at",
    "last_used_at",
}
RESPONSE_PREVIEW_LINES = 28
PARAM_DESCRIPTIONS = {  # path and query parameters FastAPI leaves undocumented
    ("path", "slug"): "The event's slug, as in its URL.",
    ("path", "pid"): "Project public id (prj_…).",
    ("path", "project_id"): "Project public id (prj_…).",
    ("path", "judge_id"): "The judge's account public id.",
    ("path", "track_id"): "Track public id (trk_…).",
    ("path", "prize_id"): "Prize public id (prz_…).",
    ("path", "user_id"): "Account public id (usr_…).",
    ("path", "criterion_id"): "Rubric criterion public id (crt_…).",
    ("path", "cid"): "Comment public id (cmt_…).",
    ("path", "hook_id"): "Webhook public id (whk_…).",
    ("path", "serial"): "Certificate serial, as printed on the certificate.",
    ("path", "code"): "The team's invite code, from its join link.",
    ("path", "name"): "Which export: projects, teams, assignments, reviews, scores or audit.",
    ("path", "kind"): "Which certificates: participation, judge or winner.",
    ("path", "action"): "publish_event, unpublish_event, open_judging, close_judging, "
    "publish_results, unpublish_results or archive.",
    ("path", "delivery_id"): "Delivery id, from the deliveries list.",
    ("path", "token_id"): "Token id, from your token list.",
    ("path", "vote_id"): "Vote id, from the suspicious-votes list.",
    ("path", "assignment_id"): "Assignment id, from the assignments list.",
    ("path", "invite_id"): "Invitation id, from the judges list.",
    ("query", "q"): "Search project titles, summaries and team names.",
    ("query", "track"): "Only projects in this track (trk_…).",
    ("query", "sort"): "newest (the default) or title.",
    ("query", "page"): "Page number, starting at 1.",
    ("query", "action"): "Only entries with this action, e.g. event.updated.",
    ("query", "code"): "Deprecated: send the code in the JSON body instead.",
    ("query", "dry_run"): "true (the default) reports what would change without writing anything.",
}
ACRONYMS = {"csv": "CSV", "json": "JSON", "api": "API", "id": "ID", "url": "URL"}
SUMMARIES = {  # where the title FastAPI derives from the function name reads badly
    ("get", "/api/v1/me"): "Current account",
    ("get", "/api/v1/events/{slug}/tally"): "Vote tally",
    ("get", "/api/v1/events/{slug}/results"): "Judged results",
    ("get", "/api/v1/events/{slug}/results/pairwise"): "Pairwise (Bradley-Terry) results",
    ("post", "/api/v1/events/{slug}/actions/{action}"): "Run a lifecycle action",
    ("post", "/api/v1/events/{slug}/assignments/auto/preview"): "Preview automatic assignment",
    ("post", "/api/v1/events/{slug}/assignments/auto/apply"): "Apply automatic assignment",
    ("get", "/api/v1/events/{slug}/audit/verify"): "Verify the audit chain",
    ("get", "/api/v1/events/{slug}/audit/anchor"): "Signed audit anchor",
    ("patch", "/api/v1/events/{slug}/voting"): "Update voting settings",
    ("get", "/api/v1/events/{slug}/webhooks/types"): "Webhook event types",
    ("post", "/api/v1/events/{slug}/webhooks/deliveries/{delivery_id}/redeliver"): (
        "Redeliver a webhook"
    ),
    ("get", "/api/v1/events/{slug}/judges/me/records"): "My judge records",
    ("get", "/api/v1/events/{slug}/export.json"): "Export an event",
    ("post", "/api/v1/events/import"): "Import an event",
}
SUCCESS_TEXT = {"200": "OK", "201": "Created", "204": "No content"}
INTERNAL_SCHEMAS = {"HTTPValidationError", "ValidationError"}


@dataclass
class Field:
    name: str
    type: str
    required: bool
    description: str
    constraints: str


@dataclass
class Param:
    name: str
    location: str
    type: str
    required: bool
    description: str
    constraints: str


@dataclass
class ResponseRow:
    status: str
    description: str
    schema: str


@dataclass
class Sample:
    key: str  # curl | python | javascript
    label: str
    text: str
    html: Markup


@dataclass
class Operation:
    key: str
    method: str
    path: str
    path_parts: list[tuple[str, bool]]
    summary: str
    description: str
    access: str
    access_kind: str  # public | signed | team | judge | organizer | admin
    public: bool
    rate_limit: str
    params: list[Param]
    body_schema: str
    body_fields: list[Field]
    success: list[ResponseRow]
    error_codes: list[str]
    curl: str
    samples: list[Sample]
    response_status: str
    response_label: str
    response_schema: str
    response_example: str | None
    response_head: Markup | None
    response_tail: Markup | None
    response_tail_lines: int
    returns: str  # json | csv | none
    search: str
    tag: str


@dataclass
class Section:
    key: str
    name: str
    description: str
    operations: list[Operation] = field(default_factory=list)


@dataclass
class SchemaDoc:
    name: str
    fields: list[Field]


@dataclass
class Reference:
    base_url: str
    sections: list[Section]
    schemas: list[SchemaDoc]
    schema_names: set[str]
    error_rows: list[tuple[str, str]]
    error_codes: list[str]
    rate_limits: list[tuple[str, str]]
    total: int


def reference(app: FastAPI, base_url: str) -> Reference:
    cached = getattr(app.state, "api_reference", None)
    if cached is None:
        cached = build(app, base_url)
        app.state.api_reference = cached
    return cached


def build(app: FastAPI, base_url: str) -> Reference:
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    routes = _dependants(app)
    sections: dict[str, Section] = {}
    for tag in schema.get("tags", []):
        sections[tag["name"]] = Section(
            key=f"tag-{tag['name']}", name=_cap(tag["name"]), description=tag.get("description", "")
        )
    error_rows: dict[str, str] = {}
    rate_limits: dict[str, str] = {}
    total = 0
    for path, item in schema.get("paths", {}).items():
        for method, op in item.items():
            if not isinstance(op, dict) or "responses" not in op:
                continue
            operation = _operation(
                method, path, op, routes.get((method, path)), components, base_url
            )
            tag = (op.get("tags") or ["other"])[0]
            sections.setdefault(tag, Section(key=f"tag-{tag}", name=_cap(tag), description=""))
            sections[tag].operations.append(operation)
            total += 1
            for status, text in _error_texts(op).items():
                error_rows.setdefault(status, text)
            if operation.rate_limit:
                rate_limits.setdefault(
                    f"{operation.method.upper()} {operation.path}", operation.rate_limit
                )
    error_schema = components.get("ErrorResponse", {})
    codes = (
        error_schema.get("properties", {})
        .get("error", {})
        .get("properties", {})
        .get("code", {})
        .get("enum", [])
    )
    schemas = [
        SchemaDoc(name, _fields(_resolve(body, components), components))
        for name, body in sorted(components.items())
        if name not in INTERNAL_SCHEMAS
    ]
    return Reference(
        base_url=base_url.rstrip("/"),
        sections=[s for s in sections.values() if s.operations],
        schemas=schemas,
        schema_names={s.name for s in schemas},
        error_rows=sorted(error_rows.items()),
        error_codes=list(codes),
        rate_limits=sorted(rate_limits.items()),
        total=total,
    )


def filter_sections(ref: Reference, query: str) -> list[Section]:
    """Sections whose operations mention the query (method, path, summary, description, tag)."""
    needle = query.strip().lower()
    if not needle:
        return ref.sections
    kept = []
    for section in ref.sections:
        ops = [op for op in section.operations if needle in op.search]
        if ops:
            kept.append(Section(section.key, section.name, section.description, ops))
    return kept


# ---- one operation ----------------------------------------------------------------------------


def _operation(method, path, op, dependant, components, base_url) -> Operation:
    public = op.get("security") == []
    calls = _calls(dependant) if dependant is not None else set()
    params = [
        Param(
            p["name"],
            p.get("in", "query"),
            _type(p.get("schema", {}), components),
            bool(p.get("required")),
            p.get("description") or PARAM_DESCRIPTIONS.get((p.get("in", "query"), p["name"]), ""),
            _constraints(p.get("schema", {})),
        )
        for p in op.get("parameters", [])
    ]
    body_schema, body_fields, example = "", [], None
    request_body = op.get("requestBody")
    if request_body:
        content = request_body.get("content", {})
        media = content.get("application/json") or (next(iter(content.values())) if content else {})
        body = media.get("schema", {})
        body_schema = _ref_name(body)
        body_fields = sorted(
            _fields(_resolve(body, components), components), key=lambda f: not f.required
        )
        example = _example(body, components, "body")
    success, error_codes = [], []
    response_example = None
    for status in sorted(op["responses"], key=lambda s: (len(s), s)):
        detail = op["responses"][status]
        if status[0] in "45":
            error_codes.append(status)
            continue
        text = detail.get("description", "")
        if text == "Successful Response":
            text = SUCCESS_TEXT.get(status, text)
        media = (detail.get("content") or {}).get("application/json") or {}
        body = media.get("schema")
        shape = (
            ""
            if body is None
            else (_ref_name(body) or ("object" if body == {} else _type(body, components)))
        )
        success.append(ResponseRow(status, text, shape))
        if response_example is None and _ref_name(body):
            value = _example(body, components, "response", full=True)
            response_example = json.dumps(value, indent=2, ensure_ascii=False)
    first = success[0] if success else ResponseRow("200", "OK", "")
    returns = "none" if first.status == "204" else ("csv" if path.endswith(".csv") else "json")
    summary = SUMMARIES.get((method, path)) or _sentence(op.get("summary") or "")
    description = " ".join((op.get("description") or "").split())
    key = "op-" + re.sub(r"[^a-z0-9]+", "-", f"{method}-{path}").strip("-")
    access, access_kind = _access(calls, public)
    curl = _curl(method, path, public, example, base_url)
    url = _url(path, base_url)
    samples = [
        Sample("curl", "curl", curl, highlight(curl)),
        _sample("python", "Python", _python(method, url, public, example, returns)),
        _sample("javascript", "JavaScript", _javascript(method, url, public, example, returns)),
    ]
    head, tail, tail_lines = None, None, 0
    if response_example is not None:
        lines = response_example.split("\n")
        head = highlight("\n".join(lines[:RESPONSE_PREVIEW_LINES]))
        if len(lines) > RESPONSE_PREVIEW_LINES:
            tail = highlight("\n".join(lines[RESPONSE_PREVIEW_LINES:]))
            tail_lines = len(lines) - RESPONSE_PREVIEW_LINES
    return Operation(
        key=key,
        method=method,
        path=path,
        path_parts=_path_parts(path),
        summary=summary,
        description=description,
        access=access,
        access_kind=access_kind,
        public=public,
        rate_limit=_rate_limit(calls),
        params=params,
        body_schema=body_schema,
        body_fields=body_fields,
        success=success,
        error_codes=error_codes,
        curl=curl,
        samples=samples,
        response_status=first.status,
        response_label=first.description,
        response_schema=first.schema if first.schema != "object" else "",
        response_example=response_example,
        response_head=head,
        response_tail=tail,
        response_tail_lines=tail_lines,
        returns=returns,
        search=f"{method} {path} {summary} {description} {(op.get('tags') or [''])[0]}".lower(),
        tag=(op.get("tags") or ["other"])[0],
    )


def _dependants(app: FastAPI) -> dict[tuple[str, str], object]:
    """(method, path) → resolved dependency tree, the same view the OpenAPI generator uses."""
    try:
        from fastapi.routing import iter_route_contexts  # FastAPI ≥ 0.141 nests included routers
    except ImportError:  # pragma: no cover - older FastAPI keeps APIRoute objects flat
        contexts = [route for route in app.routes if isinstance(route, APIRoute)]
    else:
        contexts = list(iter_route_contexts(app.routes))
    found: dict[tuple[str, str], object] = {}
    for context in contexts:
        dependant = getattr(context, "dependant", None)
        if dependant is None or not getattr(context, "include_in_schema", True):
            continue
        for method in getattr(context, "methods", None) or ():
            found[(method.lower(), context.path_format)] = dependant
    return found


def _calls(dependant) -> set:
    found = set()

    def walk(node):
        for sub in node.dependencies:
            found.add(sub.call)
            walk(sub)

    walk(dependant)
    return found


def _access(calls: set, public: bool) -> tuple[str, str]:
    """(label, kind): who may call the operation, derived from its dependency tree."""
    roles: set[Role] = set()
    for call in calls:
        roles.update(getattr(call, "roles", ()))
    if deps.require_admin in calls:
        return "Instance admin", "admin"
    if deps.require_organizer in calls:
        return "Event organizer", "organizer"
    if deps.require_submissions_open in calls:
        return "Team member, while submissions are open", "team"
    if roles:
        names = " or ".join(ROLE_LABELS.get(r, str(r)) for r in sorted(roles, key=str))
        kind = "judge" if Role.judge in roles else "team"
        return f"{names} of the event (organizers too)", kind
    if deps.require_can_create_event in calls:
        return "Signed in — instance admin, unless open event creation is enabled", "signed"
    if deps.require_user in calls:
        return "Signed in", "signed"
    return ("Public" if public else "Public; signed-in callers may see more"), "public"


def _rate_limit(calls: set) -> str:
    for call in calls:
        limit = getattr(call, "rate_limit", None)
        if limit:
            _bucket, count, window = limit
            per = "minute" if window == 60 else f"{window} s"
            return f"{count} requests per {per} per IP address"
    return ""


def _error_texts(op) -> dict[str, str]:
    return {
        status: detail.get("description", "")
        for status, detail in op["responses"].items()
        if status[0] in "45"
    }


# ---- schema helpers -----------------------------------------------------------------------------


def _ref_name(schema) -> str:
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    return ref.rsplit("/", 1)[-1] if ref else ""


def _resolve(schema, components) -> dict:
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return _resolve(components.get(_ref_name(schema), {}), components)
    if "allOf" in schema:
        merged: dict = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            part = _resolve(part, components)
            merged["properties"].update(part.get("properties", {}))
            merged["required"] += part.get("required", [])
        return merged
    return schema


def _type(schema, components) -> str:
    if not isinstance(schema, dict) or not schema:
        return "any"
    if "$ref" in schema:
        return _ref_name(schema)
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        return " or ".join(dict.fromkeys(_type(o, components) for o in options))
    if "allOf" in schema:
        return " + ".join(_type(o, components) for o in schema["allOf"])
    kind = schema.get("type")
    if kind == "array":
        return "array of " + _type(schema.get("items", {}), components)
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return "map of " + _type(extra, components)
        return "object"
    if kind is None:
        return "any"
    fmt = schema.get("format")
    return f"{kind} ({fmt})" if fmt else kind


def _num(value):
    """1.0 → 1: pydantic stores integer bounds as floats."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _constraints(schema) -> str:
    if not isinstance(schema, dict):
        return ""
    parts = []
    if "enum" in schema:
        parts.append("one of " + ", ".join(str(v) for v in schema["enum"]))
    if "maxLength" in schema:
        parts.append(f"up to {schema['maxLength']} characters")
    if "minLength" in schema and schema["minLength"] > 0:
        parts.append(f"at least {schema['minLength']} characters")
    if "minimum" in schema:
        parts.append(f"minimum {_num(schema['minimum'])}")
    if "maximum" in schema:
        parts.append(f"maximum {_num(schema['maximum'])}")
    if "minItems" in schema:
        parts.append(f"at least {schema['minItems']} items")
    default = schema.get("default")
    if default not in (None, "", []):
        parts.append(f"default {json.dumps(default)}")
    examples = schema.get("examples") or ([schema["example"]] if "example" in schema else [])
    if examples:
        parts.append(f"e.g. {json.dumps(examples[0])}")
    return "; ".join(parts)


def _fields(schema: dict, components) -> list[Field]:
    required = set(schema.get("required", []))
    rows = []
    for name, prop in schema.get("properties", {}).items():
        rows.append(
            Field(
                name,
                _type(prop, components),
                name in required,
                " ".join((prop.get("description") or "").split()) if isinstance(prop, dict) else "",
                _constraints(prop),
            )
        )
    return rows


def _example(schema, components, name: str = "", depth: int = 0, owner: str = "", full=False):
    """A plausible value for `schema`. `owner` names the schema that holds the field `name`;
    `full` includes every property (responses) instead of the required ones (request bodies)."""
    explicit = FIELD_SAMPLES.get((owner, name), _MISSING)
    if explicit is not _MISSING:
        return explicit
    if isinstance(schema, dict) and "$ref" in schema:
        target = _resolve(schema, components)
        return _example(target, components, name, depth, _ref_name(schema), full)
    schema = _resolve(schema, components)
    if schema.get("examples"):
        return schema["examples"][0]
    if "example" in schema:
        return schema["example"]
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        if name in NULL_FIELDS and any(o.get("type") == "null" for o in options):
            return None
        chosen = next((o for o in options if o.get("type") != "null"), None)
        return None if chosen is None else _example(chosen, components, name, depth, owner, full)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind in ("string", "integer", "number", "boolean"):
        if name == "id" and owner in ID_SAMPLES:
            return ID_SAMPLES[owner]
        sample = FIELD_SAMPLES.get(name, _MISSING)
        if sample is not _MISSING:
            return sample
        if schema.get("format") == "date-time":
            return DATE_SAMPLES.get(name, "2026-10-03T18:00:00Z")
    if depth > 0 and schema.get("default") not in (None, "", []):
        return schema["default"]
    if kind == "string":
        if schema.get("format") == "email" or name.endswith("email"):
            return "ada@example.org"
        if name.endswith("url"):
            return "https://example.org"
        return f"<{name}>"
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1))
    if kind == "boolean":
        return True
    if kind == "array":
        if depth >= 4:
            return []
        return [_example(schema.get("items", {}), components, name, depth + 1, owner, full)]
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        if not props and isinstance(extra, dict):
            return {"<key>": _example(extra, components, "value", depth + 1, owner, full)}
        if depth > 6:
            return {}
        if full:
            chosen = list(props)
        else:
            required = set(schema.get("required", []))
            chosen = [
                k
                for k in props
                if k in required or props[k].get("examples") or "example" in props[k]
            ] or list(props)[:3]
        return {k: _example(props[k], components, k, depth + 1, owner, full) for k in chosen}
    return None


def _url(path: str, base_url: str) -> str:
    return base_url.rstrip("/") + re.sub(
        r"\{(\w+)\}", lambda m: SAMPLE_VALUES.get(m.group(1), "{" + m.group(1) + "}"), path
    )


def _curl(method: str, path: str, public: bool, example, base_url: str) -> str:
    url = _url(path, base_url)
    lines = [f"curl {url}" if method == "get" else f"curl -X {method.upper()} {url}"]
    if not public:
        lines.append('-H "Authorization: Bearer $PODIUM_TOKEN"')
    if example is not None:
        body = json.dumps(example, ensure_ascii=False).replace("'", "'\\''")
        lines.append('-H "Content-Type: application/json"')
        lines.append(f"-d '{body}'")
    return " \\\n  ".join(lines)


def _hang(text: str, indent: int) -> str:
    """Indent every line but the first, so a multi-line literal lines up after a prefix."""
    return text.replace("\n", "\n" + " " * indent)


def _python(method: str, url: str, public: bool, example, returns: str) -> str:
    lines = ["import os", ""] if not public else []
    lines += ["import requests", ""]
    args = []
    if not public:
        args.append('headers={"Authorization": f"Bearer {os.environ[\'PODIUM_TOKEN\']}"},')
    if example is not None:
        body = pprint.pformat(example, width=72, sort_dicts=False)
        args.append("json=" + _hang(body, 9) + ",")
    if args:
        lines.append(f"response = requests.{method}(")
        lines.append(f'    "{url}",')
        lines += ["    " + arg for arg in args]
        lines.append(")")
    else:
        lines.append(f'response = requests.{method}("{url}")')
    lines.append("response.raise_for_status()")
    lines.append(
        {"json": "print(response.json())", "csv": "print(response.text)"}.get(
            returns, "print(response.status_code)"
        )
    )
    return "\n".join(lines)


def _javascript(method: str, url: str, public: bool, example, returns: str) -> str:
    options = []
    if method != "get":
        options.append(f'method: "{method.upper()}",')
    headers = []
    if not public:
        headers.append('"Authorization": `Bearer ${process.env.PODIUM_TOKEN}`,')
    if example is not None:
        headers.append('"Content-Type": "application/json",')
    if headers:
        options.append("headers: {")
        options += ["  " + h for h in headers]
        options.append("},")
    if example is not None:
        body = json.dumps(example, ensure_ascii=False)
        if len(body) > 56:  # the options loop below indents continuation lines
            body = json.dumps(example, indent=2, ensure_ascii=False)
        options.append(f"body: JSON.stringify({body}),")
    if options:
        lines = [f'const response = await fetch("{url}", {{']
        lines += ["  " + _hang(o, 2) for o in options]
        lines.append("});")
    else:
        lines = [f'const response = await fetch("{url}");']
    lines.append("if (!response.ok) throw new Error((await response.json()).error.message);")
    lines.append(
        {
            "json": "console.log(await response.json());",
            "csv": "console.log(await response.text());",
        }.get(returns, "console.log(response.status);")
    )
    return "\n".join(lines)


def _sample(key: str, label: str, text: str) -> Sample:
    return Sample(key, label, text, highlight(text))


_TOKEN = re.compile(
    r"""(?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`[^`\n]*`)"""
    r"|(?P<num>(?:(?<=: )|(?<=\[)|(?<=, )|(?<=\()|(?<==))-?\d+(?:\.\d+)?\b)"
    r"|(?P<kw>\b(?:true|false|null|True|False|None|const|await|import|print|throw|new|if)\b)"
    r"|(?P<flag>(?<=\s)-{1,2}[A-Za-z][\w-]*)"
)
_TOKEN_CLASS = {"str": "tok-s", "num": "tok-n", "kw": "tok-k", "flag": "tok-f"}


def _highlight_line(line: str) -> str:
    out: list[str] = []
    pos = 0
    for match in _TOKEN.finditer(line):
        out.append(str(escape(line[pos : match.start()])))
        kind = match.lastgroup or "str"
        css = _TOKEN_CLASS[kind]
        if kind == "str" and line[match.end() :].lstrip(" ").startswith(":"):
            css = "tok-p"  # a JSON or dict key
        out.append(f'<span class="{css}">{escape(match.group())}</span>')
        pos = match.end()
    out.append(str(escape(line[pos:])))
    return "".join(out)


def highlight(text: str) -> Markup:
    """Escape `text`, colour strings, keys, numbers, keywords and flags, and put every line in a
    block span whose class carries its indentation, so a wrapped line hangs under its own start
    instead of snapping back to the left edge. No JavaScript, no external highlighter."""
    lines = []
    for line in text.split("\n"):
        indent = min(len(line) - len(line.lstrip(" ")), 24)
        lines.append(f'<span class="ln i{indent}">{_highlight_line(line)}</span>')
    return Markup("".join(lines))


def _path_parts(path: str) -> list[tuple[str, bool]]:
    return [(seg, seg.startswith("{")) for seg in re.split(r"(\{\w+\})", path) if seg]


def _sentence(summary: str) -> str:
    words = []
    for index, word in enumerate(summary.split()):
        lower = word.lower()
        if lower in ACRONYMS:
            words.append(ACRONYMS[lower])
        elif index == 0:
            words.append(word[:1].upper() + word[1:].lower())
        else:
            words.append(lower)
    return " ".join(words)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]
