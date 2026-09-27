"""Server-rendered API reference.

Turns the OpenAPI schema plus each route's declared dependencies into flat rows a template can
print: method, path, who may call it, parameters, body fields, responses and a curl example.
Built once per process and cached on ``app.state``; the page needs no JavaScript to be read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.routing import APIRoute

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
class Operation:
    key: str
    method: str
    path: str
    path_parts: list[tuple[str, bool]]
    summary: str
    description: str
    access: str
    public: bool
    rate_limit: str
    params: list[Param]
    body_schema: str
    body_fields: list[Field]
    success: list[ResponseRow]
    error_codes: list[str]
    curl: str
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
            p.get("description", ""),
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
        body_fields = _fields(_resolve(body, components), components)
        example = _example(body, components, "body")
    success, error_codes = [], []
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
    summary = SUMMARIES.get((method, path)) or _sentence(op.get("summary") or "")
    description = " ".join((op.get("description") or "").split())
    key = "op-" + re.sub(r"[^a-z0-9]+", "-", f"{method}-{path}").strip("-")
    return Operation(
        key=key,
        method=method,
        path=path,
        path_parts=_path_parts(path),
        summary=summary,
        description=description,
        access=_access(calls, public),
        public=public,
        rate_limit=_rate_limit(calls),
        params=params,
        body_schema=body_schema,
        body_fields=body_fields,
        success=success,
        error_codes=error_codes,
        curl=_curl(method, path, public, example, base_url),
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


def _access(calls: set, public: bool) -> str:
    roles: set[Role] = set()
    for call in calls:
        roles.update(getattr(call, "roles", ()))
    if deps.require_admin in calls:
        return "Instance admin"
    if deps.require_organizer in calls:
        return "Event organizer"
    if deps.require_submissions_open in calls:
        return "Team member, while submissions are open"
    if roles:
        names = " or ".join(ROLE_LABELS.get(r, str(r)) for r in sorted(roles, key=str))
        return f"{names} of the event (organizers too)"
    if deps.require_can_create_event in calls:
        return "Signed in — instance admin, unless open event creation is enabled"
    if deps.require_user in calls:
        return "Signed in"
    return "Public" if public else "Public; signed-in callers may see more"


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
        parts.append(f"minimum {schema['minimum']}")
    if "maximum" in schema:
        parts.append(f"maximum {schema['maximum']}")
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


def _example(schema, components, name: str, depth: int = 0):
    schema = _resolve(schema, components)
    if schema.get("examples"):
        return schema["examples"][0]
    if "example" in schema:
        return schema["example"]
    if "enum" in schema:
        return schema["enum"][0]
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        for option in options:
            if option.get("type") != "null":
                return _example(option, components, name, depth)
        return None
    kind = schema.get("type")
    if depth > 0 and schema.get("default") not in (None, "", []):
        return schema["default"]
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-10-03T18:00:00Z"
        if fmt == "email" or name.endswith("email"):
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
        return [_example(schema.get("items", {}), components, name, depth + 1)] if depth < 3 else []
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        chosen = [
            k for k in props if k in required or props[k].get("examples") or "example" in props[k]
        ]
        if not chosen:
            chosen = list(props)[:3]
        return {k: _example(props[k], components, k, depth + 1) for k in chosen}
    return None


def _curl(method: str, path: str, public: bool, example, base_url: str) -> str:
    url = base_url.rstrip("/") + re.sub(
        r"\{(\w+)\}", lambda m: SAMPLE_VALUES.get(m.group(1), "{" + m.group(1) + "}"), path
    )
    lines = [f"curl {url}" if method == "get" else f"curl -X {method.upper()} {url}"]
    if not public:
        lines.append('-H "Authorization: Bearer $PODIUM_TOKEN"')
    if example is not None:
        body = json.dumps(example, ensure_ascii=False).replace("'", "'\\''")
        lines.append('-H "Content-Type: application/json"')
        lines.append(f"-d '{body}'")
    return " \\\n  ".join(lines)


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
