# scrudful 0.10.0: Semantic HTTP 0.1 at `37ec1ce` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every scrudful adapter (django-ninja, DRF, FastAPI) conform to Semantic HTTP 0.1 as merged at spec commit `37ec1ce`:
- OPTIONS answers 204 with an exact per-requester `Allow` and one HTTP Schema Resource link;
- the HSR is served by a cacheable GET that doesn't depend on the requester;
- every response carries the HSR link;
- every HSR is complete;
- no undescribed content goes to a client that doesn't accept it.

**Architecture:**
- A new stdlib-only module `scrudful/_hsr.py` holds the HSR vocabulary and helpers: link, `Allow`, serialization and ETag, the preflight test, base-URL resolution, route names.
- `_adapter.py`, `utils.py`, `entity.py`, `problem.py` and `_django.py` get small shared changes.
- Each adapter is then migrated in four tasks:
  1. a requester-independent HSR builder;
  2. the GET route;
  3. the 204 OPTIONS with `Allow`;
  4. the HSR link on every response and the framework's error responses.
- Each adapter's tasks end with a wire-conformance suite.
- Old shared functions are removed in a final cleanup task, once no adapter uses them, so the suite stays green after every task.

**Tech Stack:** Python ≥ 3.10, Django ≥ 4.2, DRF ≥ 3.15, django-ninja ≥ 1.7 < 2, FastAPI ≥ 0.100 / Starlette, pytest + pytest-django, jsonschema, pyld. Run everything with `.venv/bin/python -m pytest`.

**Spec:** `docs/superpowers/specs/2026-11-04-semantic-http-37ec1ce-design.md`, the design approved 2026-11-04. Read it first; this plan argues from it. The normative source is `~/dev/semantic-http-spec/source/*.rst` at `37ec1ce`.

## Global Constraints

- The core is stdlib-only: `scrudful/_hsr.py`, `_adapter.py`, `conditional.py`, `utils.py`, `entity.py` and `problem.py` import no framework.
- Supported versions are unchanged: Python ≥ 3.10, Django ≥ 4.2, DRF ≥ 3.15, django-ninja ≥ 1.7,<2, FastAPI ≥ 0.100.
- Every new reliance on a ninja or FastAPI internal is version-guarded, in the same way as the existing `_check_ninja_checks` / `_source_digest` guard, with a `ConfigError` (ninja) or `RuntimeError` (FastAPI) naming the internal.
- `pyproject.toml` has `filterwarnings = ["error"]`: new code must emit no warnings.
- HSR link relation: `https://w3id.org/semantic-http/ns#http-schema-resource-document`, `type="application/json"`, never an `anchor`.
- HSR GET response:
  - `application/json`;
  - `Link: <https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json>; rel="describedby"; type="application/schema+json", <https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"`;
  - a strong `ETag`;
  - `Cache-Control` defaulting to `public, max-age=300`.
- The HSR's `options` entry is exactly `{"responses": {"204": {"description": "No Content"}}}`.
- A successful non-preflight OPTIONS is `204`, with no body, `Allow`, exactly one HSR link and `Access-Control-Expose-Headers`.
- A preflight is an OPTIONS carrying both `Origin` and `Access-Control-Request-Method`. It gets `204`, no body, no Links, and runs no auth, throttle, hook, permission or load.
- Expose headers: `Link` and `Allow` always; `ETag` and `Location` when present. The public constant is `scrudful.utils.EXPOSE_HEADERS = ("ETag", "Link", "Location", "Allow")`.
- Configuration errors: shared core functions raise `scrudful._hsr.HsrConfigError` (a `ValueError`). Adapters re-raise it as `ninja.errors.ConfigError` (ninja), `django.core.exceptions.ImproperlyConfigured` (DRF) and `ValueError` (FastAPI), with the same message.
- Relative links on live response Link headers resolve against the configured base URL when one is set, and against the request otherwise (unchanged). The HSR never resolves against the request. Tasks 6, 10 and 14 apply this to their adapter's `_link` / `_link_value` / `resolve_uri` call sites, and each adds a test with a relative link: base set gives the base URL on both the HSR and the Link header.
- Django settings: one optional dict `SCRUDFUL` with keys `BASE_URL`, `HSR_ROUTE` (default `True`), `HSR_CACHE_CONTROL`, `HSR_URL` and `ERROR`. Per-router and per-viewset arguments override it.
- Version 0.10.0, breaking. Work on branch `feat/semantic-http-37ec1ce`, created from `8b07438` (0.9.0).

## Review Focus

1. **Async ninja operations.** Their `auth=` / `before=` may be `async def`, but scrudful's OPTIONS view is sync. `Allow` must still be computed correctly for an async operation, and an async check must never be silently treated as allowed. Test: Task 8.
2. **Host-agnostic bytes.** An HSR must be identical whether it is fetched through `Host: a.example`, `Host: b.example` or `X-Forwarded-Host`, by anonymous or authenticated requesters. Test: Tasks 6, 11, 15.
3. **Browser `Accept` headers.** A browser sends `text/html,application/xhtml+xml,*/*;q=0.8`, an `axios`-style client sends `application/json, text/plain, */*`, and curl sends `*/*`. The 404/500 helpers must give HTML only to the first and JSON to the other two. Test: Task 5.
4. **OPTIONS on a missing object.** A missing object still gets `204`. Object-level checks are skipped, and the loader's own 404 (scoped-rbac's unreadable object) is answered as a 404 with error Links. Test: Tasks 8, 12, 16.
5. **A preflight to a locked resource.** A preflight to a resource with `auth=`, permissions, throttles or CSRF, sent with credentials or a session cookie, is still `204`, never 401, 403, 429 or a CSRF 403. Test: Tasks 8, 12, 16.

---

## File map

| File | Change |
|---|---|
| `scrudful/_hsr.py` | **new.** HSR vocabulary and helpers (Task 2). |
| `scrudful/_adapter.py` | `merge_links` HSR slot; `describe_content` / `check_entity_links` ValueError; `prefers_html`; `expose_headers` always Link/Allow (Tasks 2–3). Remove `describe_options`, `allow_header`, `options_links` (Task 18). |
| `scrudful/utils.py` | `EXPOSE_HEADERS` (Task 3). |
| `scrudful/entity.py` | reject schema-without-context (Task 2). |
| `scrudful/problem.py` | `problem_from_errors` (Task 4). |
| `scrudful/_django.py` | `scrudful_setting`, `error_handler` (Tasks 5–6). |
| `scrudful/ninja.py` | Tasks 6–9. |
| `scrudful/rest_framework.py` | Tasks 10–13. |
| `scrudful/fastapi.py` | Tasks 14–17. |
| `tests/fixtures/semantic-http-0.1/` | re-vendored (Task 1). |
| `tests/semantic_http.py` | 37ec1ce checker (Task 1); old OPTIONS rules removed (Task 18). |
| `tests/test_hsr.py` | **new**, core (Tasks 2–5). |
| `tests/test_ninja_options.py`, `tests/test_rest_framework.py`, `tests/test_fastapi_adapter.py` | OPTIONS assertions migrated in each adapter's task 3. |
| `tests/test_ninja_hsr.py`, `tests/test_rest_framework_hsr.py`, `tests/test_fastapi_hsr.py` | **new** builder and route tests. |
| `tests/test_wire_ninja.py`, `tests/test_wire_drf.py`, `tests/test_wire_fastapi.py` | **new** wire-conformance suites. |
| `examples/**`, `tests/examples/**` | Task 19. |
| `README.rst`, `docs/source/*.rst`, `CHANGELOG.rst`, `pyproject.toml` | Task 19. |

---

### Task 1: Re-vendor the spec fixtures and teach the checker the 37ec1ce rules

**Files:**
- Modify: `tests/fixtures/semantic-http-0.1/*`, `tests/fixtures/semantic-http-0.1/README.md`
- Create: `tests/fixtures/semantic-http-0.1/problem-details.schema.json`, `problem-details.context.jsonld`
- Modify: `tests/semantic_http.py`
- Test: `tests/test_semantic_http_helpers.py`

**Interfaces:**
- Produces (in `tests/semantic_http.py`), all returning `list[str]` of problems (empty = conforms):
  - `options_204_problems(status: int, headers: Mapping[str, str], body: bytes, expected_allow: set[str] | None = None) -> list[str]`
  - `hsr_link_problems(headers: Mapping[str, str], expected_target: str | None = None) -> list[str]`: exactly one HSR link, `type="application/json"`, no `anchor`, and the target equals `expected_target` when given.
  - `hsr_get_problems(status: int, headers: Mapping[str, str], body: bytes) -> list[str]`: 200, `application/json`, the two w3id Links, `ETag`, a `Cache-Control` containing `public`, and the body valid per `validate_http_schema_resource` with `options` equal to the 204 entry.
  - `undescribed_problems(accept: str | None, headers: Mapping[str, str], hsr: dict) -> list[str]`: a response whose essence media type is no `content` key of `hsr` is a problem unless `accept` is present and accepts it (RFC 9110 §12.5.1 matching, including `*/*` and `type/*`).
  - `PROBLEM_DETAILS_SCHEMA: dict` (loaded from the fixture).
- The existing `link_problems` and `_options_problems` stay unchanged until Task 18, because adapters still use them.

- [ ] **Step 1: Copy the fixtures**

```bash
SPEC=~/dev/semantic-http-spec
git -C $SPEC rev-parse --short HEAD   # must print 37ec1ce
cp $SPEC/source/_extra/0.1/{http-schema-resource.schema.json,http-schema-resource.context.jsonld,problem-details.schema.json,problem-details.context.jsonld} tests/fixtures/semantic-http-0.1/
cp $SPEC/source/examples/person.http-schema-resource.json tests/fixtures/semantic-http-0.1/
```

In `README.md`, change the commit to `37ec1ce` and list the two problem-details files.

- [ ] **Step 2: Write the failing helper tests**

Add these to `tests/test_semantic_http_helpers.py`:

```python
HSR_LINK = '<https://h.example/_hsr/item>; rel="https://w3id.org/semantic-http/ns#http-schema-resource-document"; type="application/json"'

def test_options_204_conformant():
    h = {"Allow": "GET, HEAD, OPTIONS", "Link": HSR_LINK}
    assert options_204_problems(204, h, b"", {"GET", "HEAD", "OPTIONS"}) == []

def test_options_204_rejects_body_and_200():
    h = {"Allow": "GET, OPTIONS", "Link": HSR_LINK, "Content-Type": "application/json"}
    problems = options_204_problems(200, h, b"{}")
    assert any("content" in p for p in problems) and any("200" in p for p in problems)

def test_options_204_allow_must_equal_expected():
    h = {"Allow": "GET, PUT, OPTIONS", "Link": HSR_LINK}
    assert options_204_problems(204, h, b"", {"GET", "HEAD", "OPTIONS"})

def test_hsr_link_problems_anchor_type_count():
    assert hsr_link_problems({"Link": HSR_LINK}) == []
    assert hsr_link_problems({"Link": HSR_LINK + '; anchor="#x"'})
    assert hsr_link_problems({"Link": HSR_LINK.replace("application/json", "text/plain")})
    assert hsr_link_problems({"Link": f"{HSR_LINK}, {HSR_LINK}"})
    assert hsr_link_problems({"Link": HSR_LINK}, "https://h.example/other")

def test_hsr_get_problems_person_fixture():
    body = (FIXTURES / "person.http-schema-resource.json").read_bytes()
    h = {"Content-Type": "application/json", "Link": HSR_RESPONSE_LINKS,
         "ETag": '"x"', "Cache-Control": "public, max-age=300"}
    assert hsr_get_problems(200, h, body) == []

def test_undescribed_problems():
    hsr = json.loads((FIXTURES / "person.http-schema-resource.json").read_text())
    html = {"Content-Type": "text/html; charset=utf-8"}
    assert undescribed_problems(None, html, hsr)
    assert undescribed_problems("application/json", html, hsr)
    assert undescribed_problems("text/html,*/*;q=0.8", html, hsr) == []
    assert undescribed_problems(None, {"Content-Type": "application/json"}, hsr) == []
```

`HSR_RESPONSE_LINKS` is the w3id Link value from Global Constraints, defined as a test constant.

- [ ] **Step 3: Run the tests and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_semantic_http_helpers.py -q`
Expected: ImportError for `options_204_problems`.

- [ ] **Step 4: Implement the four helpers in `tests/semantic_http.py`**

- Reuse `parse_links` and `_header`.
- Accept matching follows RFC 9110 §12.5.1: a media range with `q=0` doesn't accept.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: everything passes. The fixture schema changed only in description strings; if a test pinned the old `person` example's `options` 200 entry, update it to the 204 entry.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/semantic-http-0.1 tests/semantic_http.py tests/test_semantic_http_helpers.py
git commit -m "tests: vendor Semantic HTTP 0.1 at 37ec1ce and check its OPTIONS, HSR link and HSR GET rules"
```

---

### Task 2: Shared HSR core (`scrudful/_hsr.py`) and the schema-without-context check

**Files:**
- Create: `scrudful/_hsr.py`
- Modify: `scrudful/_adapter.py` (`merge_links`, `describe_content`), `scrudful/entity.py`
- Test: `tests/test_hsr.py` (new), `tests/test_entity.py`, `tests/test_adapter.py`

**Interfaces:**
- Produces (`scrudful/_hsr.py`):
  - `HSR_LINK_REL: str`
  - `HSR_LINK_TYPE = "application/json"`
  - `PROBLEM_DETAILS_SCHEMA_URL = "https://w3id.org/semantic-http/0.1/problem-details.schema.json"`
  - `PROBLEM_DETAILS_CONTEXT_URL = "https://w3id.org/semantic-http/0.1/problem-details.context.jsonld"`
  - `DEFAULT_HSR_CACHE_CONTROL = "public, max-age=300"`
  - `class HsrConfigError(ValueError)`
  - `options_operation() -> dict[str, Any]`: the 204 entry, a fresh dict each call.
  - `allow_value(methods: Iterable[str]) -> str`: uppercased; `OPTIONS` is always added and `HEAD` is added iff `GET` is present; `METHOD_KEYS` order first, then any other methods in sorted order; joined with `", "`.
  - `hsr_link(url: str) -> str`: `link_content(url, HSR_LINK_REL, HSR_LINK_TYPE)`.
  - `hsr_response_links() -> str`: the value `_adapter.options_links()` returns today.
  - `serialize_hsr(body: Mapping[str, Any]) -> bytes`: `json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()`.
  - `hsr_etag(data: bytes) -> str`: `'"' + sha256(data).hexdigest()[:32] + '"'`.
  - `is_preflight(method: str, get_header: Callable[[str], str | None]) -> bool`
  - `absolute_for_hsr(url: str, base_url: str | None, what: str) -> str`: returns `url` if it is absolute; else `resolve_uri(url, base_url)` if `base_url` is given; else raises `HsrConfigError(f"{what} is the relative URL {url!r}; set a base URL (SCRUDFUL['BASE_URL'] or base_url=) so the HTTP Schema Resource does not depend on the request")`.
  - `hsr_route_name(path_template: str) -> str`:
    - segments are joined with `.`;
    - `{name}` / `{conv:name}` become `name`;
    - the empty path becomes `"_root"`;
    - e.g. `"/{pk}/notes/{note_id}"` becomes `"pk.notes.note_id"`.
  - `HSR_ROUTE_SEGMENT = "_hsr"`
- Produces (`_adapter.py`):
  - `merge_links(existing, schema, context, hsr: str | None = None)`: a view's own link with `HSR_LINK_REL` replaces scrudful's `hsr`.
  - `describe_content(...)` raises `HsrConfigError` when the media type is not in-band, `schema` is given and `context` is not.
- Produces (`entity.py`): `Entity(...)` raises `ValueError` with the same message at construction, when its links are non-callable values; a callable link is checked by `describe_content` when the HSR is built.

- [ ] **Step 1: Write failing tests in `tests/test_hsr.py`**

```python
def test_options_operation_is_204_without_content():
    assert options_operation() == {"responses": {"204": {"description": "No Content"}}}

@pytest.mark.parametrize("given, expected", [
    ({"get"}, "GET, OPTIONS, HEAD"),          # METHOD_KEYS order: get, put, post, delete, options, head, patch, trace
    ({"PUT", "patch"}, "PUT, OPTIONS, PATCH"),
    (set(), "OPTIONS"),
    ({"GET", "QUERY"}, "GET, OPTIONS, HEAD, QUERY"),
])
def test_allow_value(given, expected):
    assert allow_value(given) == expected

def test_hsr_link():
    assert hsr_link("https://h.example/_hsr/pk") == (
        '<https://h.example/_hsr/pk>; rel="https://w3id.org/semantic-http/ns#'
        'http-schema-resource-document"; type="application/json"')

def test_serialize_and_etag_are_stable():
    body = {"get": {"responses": {"200": {"description": "OK"}}}, "options": options_operation()}
    assert serialize_hsr(body) == serialize_hsr(json.loads(serialize_hsr(body)))
    assert hsr_etag(serialize_hsr(body)).startswith('"') and len(hsr_etag(b"x")) == 34

def test_is_preflight():
    h = {"origin": "https://a.example", "access-control-request-method": "PUT"}
    assert is_preflight("OPTIONS", h.get)
    assert not is_preflight("OPTIONS", {"origin": "https://a.example"}.get)
    assert not is_preflight("GET", h.get)

def test_absolute_for_hsr():
    assert absolute_for_hsr("https://x/s", None, "schema") == "https://x/s"
    assert absolute_for_hsr("/s", "https://api.example/", "schema") == "https://api.example/s"
    with pytest.raises(HsrConfigError, match="relative URL '/s'"):
        absolute_for_hsr("/s", None, "schema")

@pytest.mark.parametrize("path, name", [("/", "_root"), ("", "_root"), ("/{pk}", "pk"),
    ("/{int:pk}/notes/{note_id}", "pk.notes.note_id"), ("/stats", "stats")])
def test_hsr_route_name(path, name):
    assert hsr_route_name(path) == name

def test_describe_content_rejects_schema_without_context():
    with pytest.raises(HsrConfigError, match="x-rdf-context"):
        describe_content("application/json", "https://x/s", "application/schema+json", None, None)
    describe_content("text/turtle", "https://x/s", "application/schema+json", None, None)  # in-band: fine

def test_merge_links_view_hsr_link_wins():
    view = hsr_link("https://other/_hsr/x")
    assert merge_links(view, None, None, hsr_link("https://h/_hsr/x")) == view
```

In `tests/test_entity.py`, add:

```python
def test_entity_rejects_schema_without_context():
    with pytest.raises(ValueError, match="x-rdf-context"):
        Entity(schema_link_or_func="https://x/s")
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_hsr.py tests/test_entity.py -q`
Expected: ImportError for `scrudful._hsr`.

- [ ] **Step 3: Implement `scrudful/_hsr.py` and the `_adapter` / `entity` changes**

`_hsr.py` imports only the stdlib, `scrudful.utils` and `scrudful._adapter` (`METHOD_KEYS`, `SCHEMA_*`, `HSR_*_URL`).

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: the new tests pass. An existing test that builds a non-in-band schema-only entity now fails. Fix each such test by giving its entity a context link; never weaken the check. List them in the commit message.

- [ ] **Step 5: Commit**

```bash
git add scrudful/_hsr.py scrudful/_adapter.py scrudful/entity.py tests/
git commit -m "core: HTTP Schema Resource vocabulary and helpers; reject a schema link without an RDF context"
```

---

### Task 3: Always expose `Link` and `Allow`

**Files:**
- Modify: `scrudful/utils.py:8`, `scrudful/_adapter.py:215-221` (`expose_headers`)
- Test: `tests/test_utils.py`, plus the adapter tests that pin `Access-Control-Expose-Headers`

**Interfaces:**
- Produces:
  - `scrudful.utils.EXPOSE_HEADERS = ("ETag", "Link", "Location", "Allow")`, public and documented;
  - `EXPOSABLE_HEADERS` stays as an alias until Task 18;
  - `_adapter.expose_headers(headers)` always merges `Link` and `Allow`, plus `ETag` and `Location` when present, into an existing `Access-Control-Expose-Headers` with `merge_expose_headers`.

- [ ] **Step 1: Write the failing test**

```python
def test_expose_headers_always_link_and_allow():
    headers = FakeHeaders({})
    expose_headers(headers)
    assert headers["Access-Control-Expose-Headers"] == "Link, Allow"
    headers = FakeHeaders({"ETag": '"1"', "Access-Control-Expose-Headers": "X-Total"})
    expose_headers(headers)
    assert headers["Access-Control-Expose-Headers"] == "X-Total, ETag, Link, Allow"
```

`FakeHeaders` is a case-insensitive dict implementing `_adapter.Headers`. Reuse one from `tests/test_adapter.py` if it exists.

- [ ] **Step 2: Run the test and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_utils.py -q`
Expected: FAIL, because `Link` is not exposed when absent.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the full suite and update the pinned expose values**

Run: `.venv/bin/python -m pytest -q`
Expected: only assertions on `Access-Control-Expose-Headers` values fail. Update each to the new value (order: existing, `ETag`, `Link`, `Location`, `Allow`, as `merge_expose_headers` appends).

- [ ] **Step 5: Commit**

```bash
git commit -am "core: always expose Link and Allow to cross-origin scripts (Semantic HTTP 0.1 Browser Clients)"
```

---

### Task 4: Problem details for framework validation errors

**Files:**
- Modify: `scrudful/problem.py`
- Test: `tests/test_error_body.py`

**Interfaces:**
- Produces: `problem_from_errors(status: int, errors: Any, detail: str | None = None) -> dict[str, Any]`, returning `{"type": "about:blank", "title": reason_phrase(status), "status": status, "detail": detail or reason_phrase(status), "errors": errors}`. `errors` is omitted when it is `None`.
- Consumed by Tasks 9 (ninja 422), 13 (DRF serializer 400) and 17 (FastAPI 422). Each reshapes only when the error entity's media type essence is `application/problem+json`.

- [ ] **Step 1: Write the failing test**

```python
def test_problem_from_errors_is_valid_problem_details():
    body = problem_from_errors(422, [{"loc": ["body", "name"], "msg": "Field required"}])
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(body)
    assert body["status"] == 422 and body["title"] == "Unprocessable Content"
    assert body["errors"][0]["msg"] == "Field required"
    assert "errors" not in problem_from_errors(404, None)
```

Use `reason_phrase(422)`, whatever `_adapter.reason_phrase` returns, rather than a literal, if the phrase differs by Python version.

- [ ] **Step 2: Run the test and watch it fail** — `.venv/bin/python -m pytest tests/test_error_body.py -q`, an ImportError.
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the test and see it pass**
- [ ] **Step 5: Commit** — `git commit -am "problem: problem_from_errors for framework validation errors"`

---

### Task 5: Django 404/500 helpers that honour `Accept`

**Files:**
- Modify: `scrudful/_adapter.py` (add `prefers_html`), `scrudful/_django.py` (add `error_handler`)
- Modify: `scrudful/ninja.py`, `scrudful/rest_framework.py` (re-export `handler404`, `handler500`)
- Test: `tests/test_django_helpers.py`

**Interfaces:**
- Produces:
  - `_adapter.prefers_html(accept: str | None, media_type: str) -> bool`: true iff the best q of `text/html` (via `text/html`, `text/*` or `*/*`) is strictly greater than the best q of `media_type`'s essence (via exact, `type/*` or `*/*`). Absent or empty `accept` gives false. At equal q, the more specific range wins, as in RFC 9110 §12.5.1, so `text/html,*/*;q=0.8` is HTML and `*/*` is not.
  - `_django.error_handler(status: Literal[404, 500], error: Entity) -> Callable[..., HttpResponse]`: the returned view has Django's handler signature, `(request, exception=None)` for 404 and `(request)` for 500.
    - When `prefers_html(...)` is true, it delegates to `django.views.defaults.page_not_found` / `server_error`.
    - Otherwise its body is `problem_from_errors(status, None)` if the error media type is problem+json, else `{"detail": reason_phrase(status)}`, served as the entity's media type with the entity's Links.
    - It always sends `Vary: Accept` and runs `expose_headers`.
    - `error` must have non-callable links; otherwise it raises `TypeError("handler404/handler500 need an Entity with plain link values")` when built.
  - `scrudful.ninja.handler404(error) / handler500(error)` and `scrudful.rest_framework.handler404 / handler500` are thin re-exports, documented for `urls.py` as `handler404 = scrudful.rest_framework.handler404(ERROR)`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("accept, html", [
    ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
    ("application/json, text/plain, */*", False),
    ("*/*", False), (None, False), ("", False),
    ("application/problem+json", False), ("text/*", True),
])
def test_prefers_html(accept, html):
    assert prefers_html(accept, "application/problem+json") is html

def test_handler404_json_with_links(rf):
    view = error_handler(404, PROBLEM_ERROR)   # Entity(PROBLEM_JSON, w3id problem-details links)
    response = view(rf.get("/nowhere", HTTP_ACCEPT="application/json"), Http404())
    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/problem+json")
    assert response["Vary"] == "Accept"
    assert link_problems("GET", 404, dict(response.items()), True) == []

def test_handler500_html_for_browsers(rf):
    response = error_handler(500, PROBLEM_ERROR)(rf.get("/", HTTP_ACCEPT="text/html,*/*;q=0.8"))
    assert response.status_code == 500 and response["Content-Type"].startswith("text/html")
    assert response["Vary"] == "Accept"

def test_handler_rejects_callable_links():
    with pytest.raises(TypeError, match="plain link values"):
        error_handler(404, Entity(schema_link_or_func=lambda *a: "s", context_link_or_func="c"))
```

- [ ] **Step 2: Run the tests and watch them fail** — `.venv/bin/python -m pytest tests/test_django_helpers.py -q`
- [ ] **Step 3: Implement `prefers_html` and `error_handler`, then add the four re-exports**
- [ ] **Step 4: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 5: Commit** — `git commit -am "django: handler404/handler500 that answer JSON unless Accept prefers HTML"`

---

### Task 6: ninja — a requester-independent HSR builder

**Files:**
- Modify: `scrudful/ninja.py` (`ScrudfulRouter.__init__`, `_describe_path`, `_describe_operation`, `_media`, `_link`/`_value` describe mode, error entries)
- Modify: `scrudful/_django.py` (add `scrudful_setting`)
- Test: `tests/test_ninja_hsr.py` (new)

**Interfaces:**
- Consumes: Task 2 `options_operation`, `absolute_for_hsr`, `HsrConfigError`.
- Produces:
  - `_django.scrudful_setting(name: str, default: Any) -> Any`, which reads `settings.SCRUDFUL.get(name, default)`.
  - `ScrudfulRouter(*args, base_url: str | None = None, hsr_route: bool | None = None, hsr_url: Callable[[str], str] | None = None, hsr_cache_control: str | None = None, allowed: Callable[[HttpRequest, str, Any], bool] | None = None, **kwargs)`. `None` means the `SCRUDFUL` setting, then the default. `allowed` is stored here and used in Task 8.
  - `scrudful.ninja.http_schema_resource(router: ScrudfulRouter, path: str) -> dict[str, Any]`, which is public. `path` is the router-relative template exactly as given to the operation decorator. It raises `ConfigError` for an unknown path.
  - The builder works as follows:
    - It is cached per `(router, path)` after the first build.
    - It calls link callables as `func(None, None)`; awaitables are awaited with the existing `_run_async`.
    - It resolves every URI with `absolute_for_hsr(url, base_url, "<operation name>'s <link kind>")`.
    - It re-raises `HsrConfigError`, or any exception from a callable, as `ConfigError(f"{name}: link callables must work without a request or object when describing (called as func(None, None)): {exc}")`.
    - It lists `options` with `options_operation()`.
  - Error entries:
    - Each method has a `default` entry with the content of `config.error`, else the API-wide entity of `add_error_links`.
    - Where the API-wide entity differs from `config.error`, it adds explicit `401`, `422` and `429` entries with the API-wide content.
  - An undecorated operation on a `ScrudfulRouter` path with no `scrudful_entity=` raises `ConfigError` at `add_api_operation`:
    - `scrudful_entity` is a new keyword on `ScrudfulRouter.add_api_operation`;
    - it is reachable from `router.get(..., scrudful_entity=Entity(...))` through ninja's `**kwargs` passthrough;
    - if ninja's decorators don't pass unknown kwargs through, put it on an `@described(entity)` decorator in `scrudful.ninja` instead, and record which in the commit.
  - The existing OPTIONS operation keeps working, still a 200 with the body, until Task 8 (it now calls the new builder).

- [ ] **Step 1: Write the failing tests**

```python
def test_hsr_is_identical_across_hosts_and_requesters():
    api, router = make_api(base_url="https://api.example/")   # one @scrudful GET/PUT on "/{pk}" with relative links
    a = TestClient(router).options("/1", headers={"Host": "a.example"})
    b = TestClient(router).options("/1", headers={"Host": "b.example", "X-API-Key": ALICE})
    assert a.content == b.content
    assert http_schema_resource(router, "/{pk}")["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["$ref"] == "https://api.example/schemas/item"

def test_relative_link_without_base_url_is_config_error():
    _, router = make_api(base_url=None)
    with pytest.raises(ConfigError, match="relative URL '/schemas/item'"):
        http_schema_resource(router, "/{pk}")

def test_link_callables_are_called_in_describe_mode():
    calls = []
    def schema(request, obj):
        calls.append((request, obj)); return "https://x/s"
    _, router = make_api(schema_link_or_func=schema)
    http_schema_resource(router, "/{pk}")
    assert calls == [(None, None)] * len(calls) and calls

def test_callable_needing_request_names_itself():
    _, router = make_api(schema_link_or_func=lambda request, obj: request.build_absolute_uri("/s"))
    with pytest.raises(ConfigError, match=r"func\(None, None\)"):
        http_schema_resource(router, "/{pk}")

def test_options_entry_is_204():
    _, router = make_api()
    assert http_schema_resource(router, "/{pk}")["options"] == {"responses": {"204": {"description": "No Content"}}}

def test_default_and_explicit_error_entries():
    api, router = make_api(error=OP_ERROR); add_error_links(api, API_ERROR)
    responses = http_schema_resource(router, "/{pk}")["get"]["responses"]
    assert responses["default"]["content"] == entity_content(OP_ERROR)
    assert responses["401"]["content"] == responses["422"]["content"] == entity_content(API_ERROR)

def test_undecorated_operation_on_scrudful_router_needs_entity():
    router = ScrudfulRouter(base_url="https://api.example/")
    with pytest.raises(ConfigError, match="scrudful_entity"):
        @router.get("/plain")
        def plain(request): return {}

def test_hsr_validates_against_spec_schema():
    _, router = make_api()
    validate_http_schema_resource(http_schema_resource(router, "/{pk}"))

def test_settings_base_url(settings):
    settings.SCRUDFUL = {"BASE_URL": "https://set.example/"}
    _, router = make_api(base_url=None)
    assert "https://set.example/schemas/item" in json.dumps(http_schema_resource(router, "/{pk}"))
```

`make_api(**config)` is a module-level helper: a `NinjaAPI` with a `ScrudfulRouter(base_url=...)` holding `@scrudful` GET and PUT on `"/{pk}"`, with links `/schemas/item` and `/contexts/item` unless overridden. Reuse `Store`, `ItemOut` and the like from `tests/test_ninja_adapter.py`.

- [ ] **Step 2: Run the tests and watch them fail** — `.venv/bin/python -m pytest tests/test_ninja_hsr.py -q`
- [ ] **Step 3: Implement**

Replace `_describe_path(request, path_view)` with a request-free `_describe_path(router, path, path_view)`. Every existing caller of a link callable with a real request is unchanged; only the describe path calls `func(None, None)`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: tests in `tests/test_ninja_options.py` that relied on request-based resolution (`BASE = "http://testlocation"`) fail. Give their routers `base_url="http://testlocation/"`, so the resolved URIs are unchanged, and keep the 200-body assertions until Task 8. Tests where an undecorated plain operation now raises get `scrudful_entity=` (or `@described`).

- [ ] **Step 5: Commit** — `git commit -am "ninja: build the HTTP Schema Resource without a request (base URL, describe-mode link callables, default error entries)"`

---

### Task 7: ninja — the HSR GET route

**Files:**
- Modify: `scrudful/ninja.py` (`ScrudfulRouter`)
- Test: `tests/test_ninja_hsr.py`

**Interfaces:**
- Consumes: Task 6 `http_schema_resource`; Task 2 `serialize_hsr`, `hsr_etag`, `hsr_response_links`, `hsr_route_name`, `HSR_ROUTE_SEGMENT`.
- Produces:
  - **Registration:**
    - When `hsr_route` is true, the first scrudful operation on a path also registers `GET`/`HEAD` at `f"/{HSR_ROUTE_SEGMENT}/{hsr_route_name(path)}"` on the same router, with `auth=None`, `include_in_schema=False`, no throttles (`throttle=[]`), `url_name=f"scrudful_hsr_{name}"`, and `csrf` exemption.
    - It is registered *before* the first scrudful operation of the router, so Django resolves it ahead of `"/{pk}"`.
    - An operation whose path starts with `"/_hsr/"` raises `ConfigError("'/_hsr/' is reserved for scrudful's HTTP Schema Resources")`.
    - Two paths with the same `hsr_route_name` raise `ConfigError`.
  - **Response:** `200`, the body `serialize_hsr(...)`, `Content-Type: application/json`, `Link: hsr_response_links()`, `ETag: hsr_etag(body)`, `Cache-Control` (the router's, the setting's or `DEFAULT_HSR_CACHE_CONTROL`), and expose headers. `If-None-Match` matching the ETag (weak comparison, through `scrudful.conditional`) gives `304`.
  - **`_hsr_target(request, router, path) -> str`** is the link target that Tasks 8–9 put in responses:
    - With `hsr_url`, `hsr_url(path)`.
    - Otherwise, `request.build_absolute_uri()` of the route resolved by `url_name`, or `absolute_for_hsr` against `base_url` when that is set.
    - With `hsr_route=False` and no `hsr_url`, it raises `ConfigError("hsr_route=False needs hsr_url=")` when the router is built.
  - Mounting elsewhere: `scrudful.ninja.http_schema_resource_view(router, path) -> Callable[[HttpRequest], HttpResponse]`, a plain Django view that serves the same response.

- [ ] **Step 1: Write the failing tests**

```python
def test_hsr_route_serves_the_document():
    api, router = make_api()
    client = TestClient(api)
    r = client.get("/_hsr/pk")
    assert hsr_get_problems(r.status_code, dict(r.headers), r.content) == []
    assert json.loads(r.content) == http_schema_resource(router, "/{pk}")
    assert client.get("/_hsr/pk", headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    assert client.head("/_hsr/pk").status_code == 200

def test_hsr_route_needs_no_auth_and_ignores_throttles():
    api, _ = make_api(auth=KeyAuth(), throttle=[AnonRateThrottle("1/day")])
    client = TestClient(api)
    assert [client.get("/_hsr/pk").status_code for _ in range(3)] == [200, 200, 200]

def test_hsr_route_resolves_before_item_path():
    api, _ = make_api()
    assert TestClient(api).get("/_hsr/pk").headers["Content-Type"] == "application/json"

def test_reserved_segment_and_name_clash():
    router = ScrudfulRouter(base_url="https://api.example/")
    with pytest.raises(ConfigError, match="reserved"):
        router.get("/_hsr/x")(scrudful(...)(handler))
    # "/{pk}" and "/{id}" on one router -> names "pk" vs "id": no clash; "/a/{x}" and "/a/{x}/" -> clash

def test_hsr_route_false_needs_hsr_url():
    with pytest.raises(ConfigError, match="hsr_url"):
        make_api(hsr_route=False)

def test_http_schema_resource_view_for_mounting(rf):
    _, router = make_api(hsr_route=False, hsr_url=lambda p: f"https://docs.example/hsr{p}")
    r = http_schema_resource_view(router, "/{pk}")(rf.get("/anywhere"))
    assert hsr_get_problems(r.status_code, dict(r.items()), r.content) == []
```

- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 5: Commit** — `git commit -am "ninja: serve each path's HTTP Schema Resource by GET on ScrudfulRouter"`

---

### Task 8: ninja — the 204 OPTIONS with a per-requester `Allow`

**Files:**
- Modify: `scrudful/ninja.py` (`_options_view`, `scrudful`, `scrudful_create`, `scrudful_list` gain `allowed=`)
- Test: `tests/test_ninja_options.py` (migrate it), `tests/test_ninja_hsr.py`

**Interfaces:**
- Consumes: Task 2 `allow_value`, `hsr_link`, `is_preflight`; Task 7 `_hsr_target`.
- Produces:
  - `allowed: Callable[[HttpRequest, str, Any], bool] | None` on `scrudful`, `scrudful_create`, `scrudful_list` and `ScrudfulRouter`. The operation's own hook wins over the router's.
  - The OPTIONS order, per design §4:
    1. **Preflight:** a 204 with no body before anything else. The automatic OPTIONS operation keeps `auth=None` and gets `throttle=[]`, and the view checks `is_preflight` first. The preflight check must come before ninja's CSRF check: the operation is csrf-exempt.
    2. **Load** with the GET operation's `load` (else the first item operation's). A `None` result means no object. An `HttpError` or `Http404` raised by the loader is answered through the API's exception handler, with error Links and the HSR link.
    3. **Allow:** for each method the path serves, if an `allowed` hook exists, call `allowed(request, METHOD, obj)`. Otherwise run the operation's auth callbacks, then its `before=` hook (when present), on `copy.copy(request)` with `.method = METHOD`. An `AuthenticationError`, an `HttpError` with status 401/403/404, `Http404` or `PermissionDenied`, or a non-`None` `before` result, means not allowed. Async callbacks run with `async_to_sync`, as `_describe_path` already does. Throttles, `after_throttles` and `load` are not run per method.
    4. **Answer** 204 with `Allow: allow_value(allowed)`, `Link: hsr_link(target)`, the expose headers, and no `Content-Type`.
  - Running an operation's auth goes through ninja's `Operation._run_authentication` or the equivalent, version-guarded (Global Constraints).

- [ ] **Step 1: Write the failing tests** (in `tests/test_ninja_hsr.py`)

```python
def test_options_is_204_with_hsr_link():
    api, router = make_api()
    r = TestClient(api).options("/1", headers={"X-API-Key": ALICE})
    assert options_204_problems(r.status_code, dict(r.headers), r.content,
                                {"GET", "HEAD", "PUT", "OPTIONS"}) == []
    assert hsr_link_problems(dict(r.headers), "http://testlocation/_hsr/pk") == []

def test_allow_reflects_requester():
    api, _ = make_api(auth=KeyAuth(), before=deny_put_unless_alice)
    client = TestClient(api)
    assert client.options("/1").headers["Allow"] == "OPTIONS"
    assert client.options("/1", headers={"X-API-Key": BOB}).headers["Allow"] == "GET, OPTIONS, HEAD"
    assert client.options("/1", headers={"X-API-Key": ALICE}).headers["Allow"] == "GET, PUT, OPTIONS, HEAD"

def test_allow_hook_overrides_and_gets_object():
    seen = []
    def allowed(request, method, obj):
        seen.append((method, obj)); return method == "GET"
    api, _ = make_api(allowed=allowed)
    assert TestClient(api).options("/1").headers["Allow"] == "GET, OPTIONS, HEAD"
    assert {m for m, _ in seen} == {"GET", "PUT"} and all(o is not None for _, o in seen)

def test_async_before_is_honoured():
    api, _ = make_async_api(before=async_deny_put)
    assert "PUT" not in TestClient(api).options("/1").headers["Allow"]

def test_missing_object_still_204_without_object_checks():
    api, _ = make_api(load=lambda request, pk: None)
    assert TestClient(api).options("/404").status_code == 204

def test_loader_404_is_answered_with_error_links():
    api, _ = make_api(load=raise_http_404); add_error_links(api, API_ERROR)
    r = TestClient(api).options("/1")
    assert r.status_code == 404 and link_problems("OPTIONS", 404, dict(r.headers), True) == []
    assert hsr_link_problems(dict(r.headers)) == []

@pytest.mark.parametrize("extra", [{}, {"Cookie": "sessionid=x"}, {"X-API-Key": "wrong"}])
def test_preflight_runs_nothing(extra):
    calls = []
    api, _ = make_api(auth=CountingAuth(calls), throttle=[AnonRateThrottle("1/day")], load=counting_load(calls))
    client = TestClient(api)
    for _ in range(3):
        r = client.options("/1", headers={"Origin": "https://app.example",
                                          "Access-Control-Request-Method": "PUT", **extra})
        assert r.status_code == 204 and r.content == b"" and "Link" not in r.headers
    assert calls == []

def test_options_consumes_no_throttle_for_allow():
    api, _ = make_api(throttle=[AnonRateThrottle("2/day")])
    client = TestClient(api)
    client.options("/1")
    assert client.get("/1").status_code == 200   # the per-method checks ran no throttle
```

- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Migrate `tests/test_ninja_options.py`**
  - Every assertion of a 200 body becomes `http_schema_resource(router, path)` or a GET of `/_hsr/<name>`.
  - Every `Allow == allow_header(body)` becomes the expected per-requester set.
  - Every `options_links()` Link assertion becomes `hsr_link_problems`.
  - Keep each test's intent. Delete only the tests whose subject no longer exists (for example "OPTIONS body rendered by the API's renderer"), and replace each with its GET-route equivalent where the property still applies.
- [ ] **Step 5: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 6: Commit** — `git commit -am "ninja: OPTIONS answers 204 with an exact per-requester Allow and the HTTP Schema Resource link"`

---

### Task 9: ninja — the HSR link everywhere, a JSON 405, problem 422s, and the wire suite

**Files:**
- Modify: `scrudful/ninja.py` (`_add_links`, `_early_response`, `_error_links`, `_raised_links_run`, `add_error_links`, `ScrudfulRouter` 405)
- Create: `tests/test_wire_ninja.py`
- Test: `tests/test_ninja_errors.py`, `tests/test_wire_ninja.py`

**Interfaces:**
- Consumes: Task 2 `merge_links(..., hsr=)`; Task 4 `problem_from_errors`; Task 7 `_hsr_target`.
- Produces:
  - **The HSR link on every response** scrudful builds or decorates on a described path: 2xx, 304, early 400/412/428, errors raised in an operation, errors through `add_error_links`, and HEAD.
  - **JSON 405.** A `ScrudfulRouter` path answers a method it doesn't serve with a JSON 405 (the API-wide error entity's media type and Links when `add_error_links` was called, else `{"detail": "Method not allowed"}` as `application/json`), with `Allow` listing the methods served (RFC 9110, not per requester) and the HSR link. This uses a `PathView` subclass, or ninja's `_not_allowed` hook, version-guarded.
  - **Problem 422.** When the error entity is problem+json, `add_error_links` reshapes ninja's validation 422 body with `problem_from_errors(422, body["detail"], "Request validation failed")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_response_carries_the_hsr_link(api_and_cases):   # 200, HEAD, 304, 400, 412, 428, raised 404, 401, 422
    for method, url, headers, status in CASES:
        r = client.generic(method, url, headers=headers)
        assert r.status_code == status
        assert hsr_link_problems(dict(r.headers), "http://testlocation/_hsr/pk") == []

def test_unrouted_method_is_json_405():
    r = client.generic("TRACE", "/1", headers={"Accept": "application/json"})
    assert r.status_code == 405 and r.headers["Content-Type"].startswith("application/")
    assert r.headers["Allow"] == "GET, PUT, OPTIONS, HEAD"
    assert undescribed_problems("application/json", dict(r.headers), hsr) == []

def test_validation_422_is_problem_details():
    add_error_links(api, PROBLEM_ERROR)
    r = client.put("/1", json={"bad": True}, headers=AUTH_PRECONDITIONS)
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(r.json())
    assert r.json()["errors"]
```

In `tests/test_wire_ninja.py`, port the audit's checks (`/private/tmp/claude-501/-Users-davidcharboneau-dev/26f8c9d0-4682-4a25-b1b1-23f1f0934b34/scratchpad/ninja/agree.py`, `validate.py` and the `wire*.out` scenarios) onto the `examples/ninja` API, extended with an authenticated router. It has one parametrized test per rule family:
- `test_described_content_links`
- `test_agreement`
- `test_options`
- `test_hsr_get`
- `test_hsr_invariance` (two Hosts × two requesters give identical bytes)
- `test_allow_per_requester`
- `test_preflight`
- `test_undescribed` (Accept absent and `application/json`, across 404/405/500 with `DEBUG=False` and the Task 5 handlers installed)
- `test_expose_headers`

Every case asserts that the problem list is empty.

- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass, with updated Link assertions where a test pinned the exact Link value.
- [ ] **Step 5: Commit** — `git commit -am "ninja: HTTP Schema Resource link on every response, JSON 405, problem-details 422; wire-conformance suite"`

---

### Task 10: DRF — a requester-independent HSR builder

**Files:**
- Modify: `scrudful/rest_framework.py` (`ScrudfulMetadata`, `_link_value`, `_entity_links`, `_media`, `_content`, `scrudful_viewset` Meta reading, `@scrudful` / `ScrudfulViewFunc` options)
- Test: `tests/test_rest_framework_hsr.py` (new)

**Interfaces:**
- Consumes: Task 2; Task 6 `_django.scrudful_setting`.
- Produces:
  - `scrudful.rest_framework.http_schema_resource(view_func: Callable[..., Any]) -> dict[str, Any]`, which is public. `view_func` is what `as_view(...)` or `scrudful_api_view` returns. It builds a request-less view instance as `view_func.cls(**view_func.initkwargs)`; for a viewset it sets `view.action_map = view_func.actions`, `view.action = <the action for each method>`, `view.args = ()`, `view.kwargs = {}` and `view.request = None`. The result is cached per `view_func`.
  - Link callables are called as `func(view, None)` in describe mode. Errors are re-raised as `ImproperlyConfigured`, with a message naming the callable and `func(view, None)`.
  - URIs go through `absolute_for_hsr` against `Meta.base_url` / `scrudful(base_url=)` / `SCRUDFUL["BASE_URL"]`.
  - New options:
    - `Meta.statuses` / `@scrudful(statuses=...)`: `Mapping[str, Sequence[int]]` (method name → success statuses). The default is `{"post": [201], "delete": [204]}`, else `[200]`.
    - `Meta.entity` / `@scrudful(entity=...)`: an `Entity` for success content, replacing `_VIEW_MEDIA_TYPE`. It defaults to an `application/json` entity built from the existing `schema_*` / `context_*` options.
    - `Meta.allowed_func` / `@scrudful(allowed_func=...)`: `(view, request, method, obj) -> bool`, stored and used in Task 12.
  - Error entries: `default` uses the handler's `error` (else `SCRUDFUL["ERROR"]`). Where those differ, explicit `401`, `403`, `406` and `415` entries carry `SCRUDFUL["ERROR"]`'s content.
  - `ScrudfulMetadata.determine_metadata(request, view)` keeps working (DRF calls it) by returning `http_schema_resource(...)` for the view's resolved `view_func` (`request.resolver_match.func`).

- [ ] **Step 1: Write the failing tests**

```python
def test_hsr_independent_of_request(settings):
    settings.SCRUDFUL = {"BASE_URL": "https://api.example/"}
    func = ItemViewSet.as_view({"get": "retrieve", "put": "update"}, resource_type_name="item")
    body = http_schema_resource(func)
    validate_http_schema_resource(body)
    assert body["options"] == {"responses": {"204": {"description": "No Content"}}}
    assert body == http_schema_resource(func)   # cached and stable

def test_describe_mode_view_has_initkwargs():
    seen = []
    class V(viewsets.ModelViewSet):
        class Meta:
            schema_link_or_func = staticmethod(lambda view, request, **kw: seen.append((view.resource_type_name, request)) or "https://x/s")
            context_link_or_func = "https://x/c"
    scrudful_viewset(V)
    http_schema_resource(V.as_view({"get": "retrieve"}, resource_type_name="note"))
    assert seen and set(seen) == {("note", None)}

def test_statuses_option():
    body = http_schema_resource(ActionView.as_view({"post": "ping"}))   # Meta.statuses = {"post": [200]}
    assert set(body["post"]["responses"]) >= {"200"} and "201" not in body["post"]["responses"]

def test_entity_option_describes_csv():
    body = http_schema_resource(CsvView.as_view())   # Meta.entity = Entity(media_type="text/csv", csvw links)
    assert "text/csv" in body["get"]["responses"]["200"]["content"]

def test_default_and_explicit_error_entries(settings): ...
    # handler error=OP_ERROR, SCRUDFUL["ERROR"]=API_ERROR: default -> OP_ERROR, "401"/"403" -> API_ERROR
```

- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: tests in `tests/test_rest_framework.py` that relied on request-based URIs fail. Set `SCRUDFUL = {"BASE_URL": "http://testserver/"}` in those tests (with the `settings` fixture), so the resolved values are unchanged. Keep the OPTIONS 200-body assertions until Task 12.

- [ ] **Step 5: Commit** — `git commit -am "drf: build the HTTP Schema Resource without a request (describe-mode view, statuses=, entity=, error entries)"`

---

### Task 11: DRF — `hsr_urls()`

**Files:**
- Modify: `scrudful/rest_framework.py`; `examples/scrudful_rest_framework/scrudful_rest_framework/urls.py`
- Test: `tests/test_rest_framework_hsr.py`

**Interfaces:**
- Consumes: Task 10 `http_schema_resource`; Task 2 serialization, `hsr_response_links`.
- Produces:
  - `scrudful.rest_framework.hsr_urls() -> list[URLPattern]`: one pattern, `path("_hsr/<str:view_name>", hsr_view, name="scrudful_hsr")`. The view needs no auth, and it is csrf-exempt and never throttled (a plain Django view, not an `APIView`).
  - On the first request, `hsr_view` scans `get_resolver()` recursively, including `include()` and namespaces, and maps each pattern's full `view_name` (namespace-qualified, e.g. `"api:item-detail"`) to its callback when the callback is a scrudful view.
    - A scrudful view is one whose `cls` went through `scrudful_viewset`, or that has a `ScrudfulViewFunc` handler, or that came from `scrudful_api_view`.
    - Every scrudful view needs a URL name; an unnamed one raises `ImproperlyConfigured` naming its route.
  - The response has the same headers as Task 7's route. An unknown `view_name` gives a 404 through the Task 5 handler's JSON.
  - `_hsr_target(request) -> str`:
    - `Meta.hsr_url(view_name)` / `SCRUDFUL["HSR_URL"](view_name)` when given;
    - else `reverse("scrudful_hsr", kwargs={"view_name": request.resolver_match.view_name})`, made absolute against `BASE_URL` (or against the request when that is unset);
    - when the reverse fails (no include) and there's no `HSR_URL`, it raises `ImproperlyConfigured("add path('', include(scrudful.rest_framework.hsr_urls())) to your root URLconf, or set SCRUDFUL['HSR_URL']")`.
  - `Meta.hsr_route = False` / `SCRUDFUL["HSR_ROUTE"] = False` takes that view out of `hsr_urls` (its HSR 404s there) and requires an `HSR_URL`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture
def urls(settings):   # a test URLconf: a DRF DefaultRouter, a hand-mounted path(), a scrudful_api_view, a namespaced include, + hsr_urls()
    settings.ROOT_URLCONF = "tests.drf_hsr_urls"

def test_hsr_urls_serves_every_scrudful_view(urls, client):
    for view_name in ["item-detail", "item-list", "notes", "api:thing-detail", "stats"]:
        r = client.get(f"/_hsr/{view_name}")
        assert hsr_get_problems(r.status_code, dict(r.items()), r.content) == []

def test_hsr_identical_across_hosts(urls, client, settings):
    settings.ALLOWED_HOSTS = ["a.example", "b.example"]
    assert client.get("/_hsr/item-detail", HTTP_HOST="a.example").content == \
           client.get("/_hsr/item-detail", HTTP_HOST="b.example").content

def test_unnamed_scrudful_view_is_improperly_configured(settings, client): ...
def test_missing_include_is_improperly_configured(settings, client): ...   # OPTIONS on a view -> ImproperlyConfigured
def test_hsr_route_false_with_hsr_url(settings, client): ...
```

Create `tests/drf_hsr_urls.py` as the URLconf fixture module.

- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement, and add the include to the example's `urls.py`**
- [ ] **Step 4: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 5: Commit** — `git commit -am "drf: hsr_urls() serves every scrudful view's HTTP Schema Resource"`

---

### Task 12: DRF — the 204 OPTIONS with a per-requester `Allow`

**Files:**
- Modify: `scrudful/rest_framework.py` (`_options`, `_install_metadata`, the viewset `initial` override)
- Test: `tests/test_rest_framework_hsr.py`, migrate `tests/test_rest_framework.py`'s OPTIONS tests

**Interfaces:**
- Consumes: Task 2; Task 10 `allowed_func`; Task 11 `_hsr_target`.
- Produces: scrudful's installed `options` and an `initial()` wrapper on scrudful views:
  1. **Preflight:** `dispatch` answers 204 before `initial()` runs. The check is in a `dispatch` wrapper, so no authentication, throttling, permission or CSRF runs.
  2. **Authenticate, don't gate:** for OPTIONS, `initial()` performs authentication (`perform_authentication`) and throttles (`check_throttles`), but skips `check_permissions`.
  3. **Load:** for a detail route (one whose `lookup_url_kwarg` / `lookup_field` is in `kwargs`), `obj = view.get_object()` with `check_object_permissions` suppressed. `Http404` means `obj = None`. Any other `APIException` is answered through `handle_exception` (error Links and the HSR link).
  4. **Allow:** for each method of `view.allowed_methods` (minus OPTIONS), `allowed_func(view, request, method, obj)` if one is given. Otherwise `r = clone_request(request, method)` (from `rest_framework.request`), then `view.check_permissions(r)` and, if `obj` is not `None`, `view.check_object_permissions(r, obj)`. A `PermissionDenied` or `NotAuthenticated` means not allowed.
  5. **Answer:** a 204 `Response()` with no data, `Allow: allow_value(...)` (also written to `view.headers["Allow"]`, so DRF's `finalize_response` keeps it), `Link: hsr_link(_hsr_target(request))` and the expose headers.
- 405s keep DRF's `Allow` (all served methods).

- [ ] **Step 1: Write the failing tests**

```python
def test_options_204(api_client):
    r = api_client.options("/items/1/")
    assert options_204_problems(r.status_code, dict(r.items()), r.content) == []
    assert hsr_link_problems(dict(r.items()), "http://testserver/_hsr/item-detail") == []

@pytest.mark.parametrize("user, allow", [(None, "GET, OPTIONS, HEAD"),
    ("reader", "GET, OPTIONS, HEAD"), ("writer", "GET, PUT, DELETE, OPTIONS, HEAD, PATCH")])
def test_allow_per_requester(api_client, user, allow):   # /x/perm-items/1/ with IsAuthenticatedOrReadOnly + object perms
    if user: api_client.force_authenticate(User.objects.get(username=user))
    assert api_client.options("/x/perm-items/1/").headers["Allow"] == allow

def test_object_level_denial_excluded(api_client): ...   # writer, but object owned by someone else -> no PUT
def test_anonymous_on_locked_resource_gets_204_allow_options(api_client):   # IsAdminUser view
    r = api_client.options("/x/admin-items/1/")
    assert r.status_code == 204 and r.headers["Allow"] == "OPTIONS"
def test_missing_object_is_204(api_client): ...
@pytest.mark.parametrize("extra", [{}, {"HTTP_COOKIE": "sessionid=x"}])
def test_preflight_runs_nothing(api_client, extra): ...   # admin view + throttle "1/day": three preflights -> 204, no Link
def test_allowed_func_overrides(api_client): ...
```

- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Migrate `tests/test_rest_framework.py`'s OPTIONS assertions**, as in Task 8 Step 4: body assertions move to `http_schema_resource(...)` or `/_hsr/...`.
- [ ] **Step 5: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 6: Commit** — `git commit -am "drf: OPTIONS answers 204 with an exact per-requester Allow and the HTTP Schema Resource link"`

---

### Task 13: DRF — the HSR link everywhere, problem 400s, and the wire suite

**Files:**
- Modify: `scrudful/rest_framework.py` (`ScrudfulViewFunc._handle` / `_early_response`, `exception_handler`, `link_header`, `error_link_header`)
- Create: `tests/test_wire_drf.py`
- Test: `tests/test_rest_framework.py`, `tests/test_wire_drf.py`

**Interfaces:**
- Consumes: Task 2 `merge_links(..., hsr=)`; Task 4; Task 11 `_hsr_target`.
- Produces:
  - **The HSR link** on every response scrudful decorates: success, 304, early 400/412/428, `exception_handler` errors on scrudful views, and HEAD.
  - **Problem 400s.** With a problem+json error entity, `exception_handler` reshapes a `ValidationError`'s data with `problem_from_errors(400, data, "Request validation failed")`.
  - **Browsable API.** Every scrudful response sends `Vary: Accept` when the view's renderer classes include a non-JSON renderer. There is no other change; the docs carry the recommendation (Task 19).

- [ ] **Step 1: Write the failing tests**
  - `test_every_response_carries_the_hsr_link`, with the cases 200, HEAD, 304, 400, 412, 428, serializer 400, 403, 404, 405 and 406.
  - `test_serializer_400_is_problem_details`.
  - `test_browsable_renderer_sends_vary_accept`.
  - `tests/test_wire_drf.py`, ported from the audit's `drf/harness/{run,extra,probe2,probe3}.py` with the same rule families as Task 9, plus `test_undescribed` with `DEBUG=False`, `handler404`/`handler500` from Task 5, and `?format=api` documented as excluded.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 5: Commit** — `git commit -am "drf: HTTP Schema Resource link on every response, problem-details 400; wire-conformance suite"`

---

### Task 14: FastAPI — a requester-independent HSR builder

**Files:**
- Modify: `scrudful/fastapi.py` (`add_options`, `_describe`, `_describe_route`, `_description_kwargs`, `scrudful` gains `entity=`, `_ENDPOINT_MEDIA_TYPE` use)
- Test: `tests/test_fastapi_hsr.py` (new)

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - `add_options(app, *, base_url: str | None = None, hsr_route: bool = True, hsr_url: Callable[[str], str] | None = None, hsr_cache_control: str | None = None, allowed: Callable[[Request, str, Any], bool | Awaitable[bool]] | None = None, load: Callable[..., Any] | None = None) -> None`. `allowed` and `load` are stored and used in Task 16.
  - `scrudful.fastapi.http_schema_resource(app: FastAPI | APIRouter, path: str) -> dict[str, Any]`, which is public:
    - it is keyed by the full route path string;
    - it is built without a request;
    - it is cached;
    - it raises `ValueError` for an unknown path.
  - Link callables are called with no arguments in describe mode, so `func()` is used even when the endpoint's callables declare endpoint kwargs. A callable whose required parameters can't be satisfied raises `ValueError` naming it and `func()`.
  - `@scrudful(entity: Entity | None = None)` sets the success media type and links; it defaults to `application/json` from the existing link options.
  - Each route's `responses=` statuses and their `content` media types are described, and so is `status_code`.
  - Error entries: `default` takes the content of the `add_error_links` entity. With FastAPI, error entries come only from `add_error_links`.

- [ ] **Step 1: Write the failing tests**
  - `test_hsr_identical_across_hosts`: `http_schema_resource(app, "/users/{id}")` equals the body served at `Host: a` and at `Host: b` (once Task 15 lands; until then, compare two builds).
  - `test_relative_link_without_base_url_raises`.
  - `test_link_callables_called_without_arguments`.
  - `test_entity_option_csv`: `text/csv` appears as the content key, with `x-schema`.
  - `test_responses_kwarg_described`: `responses={404: {...}}` gives a `"404"` entry.
  - `test_status_code_201_from_route`.
  - `test_default_error_entry_from_add_error_links`.
  - `test_options_entry_is_204`.
  - `test_validates`.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite** — set `base_url="http://testserver/"` in existing `add_options` tests so their URIs are unchanged; keep the 200-body assertions until Task 16.
- [ ] **Step 5: Commit** — `git commit -am "fastapi: build the HTTP Schema Resource without a request (base URL, entity=, responses=, default error entry)"`

---

### Task 15: FastAPI — the HSR GET route

**Files:**
- Modify: `scrudful/fastapi.py`
- Test: `tests/test_fastapi_hsr.py`

**Interfaces:**
- Consumes: Task 14.
- Produces:
  - **Registration:** with `hsr_route`, `add_options` adds a `GET`/`HEAD` route at `f"/_hsr/{hsr_route_name(path)}"` for each scrudful path, with no dependencies (added as a Starlette `Route`, not an `APIRoute`, so app-level dependencies don't apply) and `include_in_schema=False`. It is inserted *before* the app's existing routes, so it resolves ahead of `"/{id}"`.
  - **Errors:** a scrudful path starting with `/_hsr/` raises `ValueError` (reserved), and so does a name clash.
  - **Response:** the same headers and 304 behaviour as Task 7.
  - `_hsr_target(request, path) -> str` follows the same rules as Task 7; with `hsr_route=False` and no `hsr_url`, `add_options` raises `ValueError`.
  - `scrudful.fastapi.http_schema_resource_endpoint(app, path) -> Callable[[Request], Awaitable[Response]]`, for mounting elsewhere.

- [ ] **Step 1: Write the failing tests**
  - `test_hsr_route_serves_document`: `hsr_get_problems`, 304 and HEAD.
  - `test_hsr_route_has_no_dependencies`: an app-level `Depends(require_token)` doesn't apply to it.
  - `test_hsr_bytes_identical_across_hosts_and_tokens`.
  - `test_reserved_segment`.
  - `test_hsr_route_false_needs_hsr_url`.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 5: Commit** — `git commit -am "fastapi: serve each path's HTTP Schema Resource by GET from add_options"`

---

### Task 16: FastAPI — the 204 OPTIONS with a per-requester `Allow`

**Files:**
- Modify: `scrudful/fastapi.py` (`_options_endpoint`, `_options_response`)
- Test: `tests/test_fastapi_hsr.py`, migrate `tests/test_fastapi_adapter.py`'s OPTIONS tests

**Interfaces:**
- Consumes: Task 14 (`allowed`, `load`), Task 15 (`_hsr_target`).
- Produces the OPTIONS order:
  1. **Preflight:** 204, no Links, nothing else run.
  2. **Load:** `obj = await maybe_await(load(request, **path_params))` when `load` is given; `None` without one. An `HTTPException` from `load` is answered through the app's exception handlers.
  3. **Allow:** per method, `allowed(request, METHOD, obj)` if given. Otherwise, resolve the route's security dependencies on a scope copy with `"method": METHOD`. Those are the `Security(...)` dependencies and those in the route's `dependencies=` and its routers' and app's, but no parameter, body or plain `Depends` sub-dependency that reads the body; use `fastapi.dependencies.utils.solve_dependencies`, version-guarded. An `HTTPException` with 401/403/404 means not allowed.
  4. **Answer:** a 204 `Response()` with `Allow`, `Link: hsr_link(...)` and the expose headers.

- [ ] **Step 1: Write the failing tests**
  - `test_options_204`.
  - `test_allow_per_requester`: anonymous gets `"OPTIONS"`; with a reader token, `GET, OPTIONS, HEAD`; with an admin token, it includes `PUT`. Its routes use `dependencies=[Security(require_role("writer"))]` on PUT.
  - `test_load_and_allowed_hook_get_object`.
  - `test_missing_object_204_without_load_result`.
  - `test_load_http_exception_answered_with_error_links`.
  - `test_preflight_runs_nothing`, with a counting dependency.
  - `test_body_reading_dependency_is_skipped_and_method_allowed`.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Migrate `tests/test_fastapi_adapter.py`'s OPTIONS assertions**, as in Task 8 Step 4.
- [ ] **Step 5: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 6: Commit** — `git commit -am "fastapi: OPTIONS answers 204 with a per-requester Allow and the HTTP Schema Resource link"`

---

### Task 17: FastAPI — the HSR link everywhere, kept dependency Links, a JSON 500, problem 422s, and the wire suite

**Files:**
- Modify: `scrudful/fastapi.py` (`_conditional_response` at `:1384-1430`, the decorator's Link merge, `add_error_links`)
- Create: `tests/test_wire_fastapi.py`
- Test: `tests/test_fastapi_adapter.py`, `tests/test_wire_fastapi.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 15.
- Produces:
  - **The HSR link** on every response scrudful decorates, and on errors through `add_error_links` for described paths.
  - **Dependency Links kept.** The early 400/412/428 keeps the injected `Response`'s `Link` lines, merged by relation through `merge_links`, alongside `Set-Cookie`.
  - **JSON 500.** `add_error_links` also registers a handler for `Exception`, which answers a JSON 500 with the entity's media type and Links (problem details when problem+json) and the HSR link. It doesn't replace a handler the app already registered for `Exception`, which it wraps instead.
  - **Problem 422.** With problem+json, `RequestValidationError` bodies are reshaped with `problem_from_errors(422, exc.errors(), "Request validation failed")`.

- [ ] **Step 1: Write the failing tests**
  - `test_every_response_carries_the_hsr_link`, with the cases 200, HEAD, 304, 400, 412, 428, raised 404, 401, 403, 405, 422 and 500.
  - `test_dependency_link_kept_on_412`.
  - `test_500_is_json_with_links`, using `TestClient(app, raise_server_exceptions=False)`.
  - `test_422_problem_details`.
  - `tests/test_wire_fastapi.py`, ported from `fastapi/harness.py` and `check_{example,minimal,accept}.py` with the Task 9 rule families. The CORS cases use `CORSMiddleware(allow_methods=["*"], expose_headers=list(EXPOSE_HEADERS))`.
- [ ] **Step 2: Run the tests and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 5: Commit** — `git commit -am "fastapi: HTTP Schema Resource link on every response, kept dependency Links, JSON 500, problem-details 422; wire-conformance suite"`

---

### Task 18: Remove the `bcc338e` remnants

**Files:**
- Modify: `scrudful/_adapter.py` (remove `describe_options`, `allow_header`, `options_links`), `scrudful/utils.py` (remove `EXPOSABLE_HEADERS`), `tests/semantic_http.py` (remove `_options_problems`; `link_problems` routes OPTIONS through `options_204_problems`)

- [ ] **Step 1: Delete the functions, and run a search that must print nothing**

Run: `grep -rn "describe_options\|allow_header\|options_links\|EXPOSABLE_HEADERS\|_options_problems" scrudful tests examples docs README.rst`
Expected: no output, after replacing any remaining use with its Task 2/3 equivalent.

- [ ] **Step 2: Run the full suite** — `.venv/bin/python -m pytest -q`, expecting it to pass.
- [ ] **Step 3: Commit** — `git commit -am "Remove the Semantic HTTP bcc338e OPTIONS helpers"`

---

### Task 19: Examples, docs, CHANGELOG and version 0.10.0

**Files:**
- Modify: `examples/ninja/**`, `examples/scrudful_rest_framework/**`, `examples/fastapi/api.py`, `tests/examples/**`
- Modify: `README.rst`, `docs/source/ninja.rst`, `docs/source/rest_framework.rst`, `docs/source/fastapi.rst`, `CHANGELOG.rst`, `pyproject.toml`

- [ ] **Step 1: Update the examples**
  - Each sets `SCRUDFUL["BASE_URL"]` / `base_url=` and installs `handler404`/`handler500` (Django).
  - The DRF example adds `include(hsr_urls())`.
  - The FastAPI example routes HEAD and calls `add_options(app, base_url=..., load=...)`.
  - Each example's test (`tests/examples/*/test_api.py`) runs `tests/test_wire_<adapter>.py`'s checkers against the example and asserts no problems.
- [ ] **Step 2: Run the example tests** — `.venv/bin/python -m pytest tests/examples -q`, expecting a pass.
- [ ] **Step 3: Update the docs**
  - Replace every description of the OPTIONS body or "Allow lists the body's methods", and check with:

    `grep -rn "Allow.*body\|OPTIONS.*body\|HTTP Schema Resource.*OPTIONS response" README.rst docs/source`

    Only lines describing the new model may remain.
  - Add, per adapter: settings (`SCRUDFUL` keys), the HSR route and mounting elsewhere, the `allowed` hook and its defaults, the describe-mode rule for link callables, the error helpers, and CORS middleware settings (`expose_headers=scrudful.utils.EXPOSE_HEADERS`, `allow_methods` including OPTIONS, the middleware covering `/_hsr/`).
  - Add a "Migrating from 0.9" section.
  - Correct the form-parser statement (now a MAY) and the browsable-API statement (allowed for `Accept: text/html`, with `Vary: Accept`).
- [ ] **Step 4: Write CHANGELOG 0.10.0**
  - Use the existing style: "Behaviour change" sections per adapter, then "New", then "Removed".
  - One line per changed wire behaviour and API, each traceable to a Task 2–18 commit:
    - OPTIONS 204;
    - `Allow` semantics;
    - the HSR route and its reserved `_hsr` segment;
    - the HSR link on every response;
    - expose headers;
    - the `ConfigError`/`ImproperlyConfigured`/`ValueError` cases;
    - `statuses=` / `entity=` / `allowed` / `load=`;
    - the JSON 405 and 500;
    - problem-details reshaping;
    - the removed internals.
- [ ] **Step 5: Bump the version** — `uv version 0.10.0`
- [ ] **Step 6: Build the docs and run everything**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m flake8 scrudful tests && (cd docs && make html SPHINXOPTS=-W)`
Expected: every test passes, flake8 reports nothing and Sphinx gives no warnings. If `docs/Makefile` doesn't exist, use the docs build command from `.gitlab-ci.yml`.

- [ ] **Step 7: Commit** — `git commit -am "0.10.0: Semantic HTTP 0.1 at 37ec1ce (docs, examples, changelog)"`
