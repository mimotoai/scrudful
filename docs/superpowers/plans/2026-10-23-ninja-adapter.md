# scrudful django-ninja adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `scrudful.ninja`, which makes django-ninja operations
semantic-http-spec compliant: conditional requests computed only from
authorized objects, scrud Link headers, create and general POST, the scrud
list envelope, and automatic OPTIONS.

**Architecture:** The ninja adapter wraps the endpoint function, not
`Operation.run`, so it runs after ninja's auth and throttling. Request
handling is in this order:

1. A `load` callable supplies the authorized object.
2. The validators and `evaluate_preconditions` (`scrudful/conditional.py`)
   decide early responses.
3. Headers go on ninja's temporal `HttpResponse`.

A `ScrudfulRouter` (a ninja `Router` subclass, like ninja's own
`RouterPaginated`) adds the OPTIONS operation to every path. Pieces shared
with DRF move into private modules:

- Django request/response helpers go to `scrudful/_django.py`.
- The OPTIONS description pieces go to `scrudful/_adapter.py`.

DRF behaviour does not change.

**Tech Stack:** Python 3.10–3.14, Django ≥ 4.2, django-ninja ≥ 1.6 (1.7.1 is
locked), pydantic 2, pytest + pytest-django, uv.

**Spec:** `docs/superpowers/specs/2026-10-23-ninja-adapter-design.md`
(approved). Read it before starting any task. Section references below (§1,
§5, …) point to it.

## Global Constraints

- Python 3.10–3.14. CI runs `uv run --locked -p "$PYTHON" pytest -q` on each.
- The package has no runtime dependencies. Add the optional extra
  `ninja = ["django>=4.2", "django-ninja>=1.6"]`.
- `scrudful/conditional.py`, `scrudful/_adapter.py`, `scrudful/utils.py` and
  the new `scrudful/collection.py` are stdlib-only (no Django, no ninja).
- `scrudful.ninja` must import without DRF installed. `scrudful/_django.py`
  must not import DRF or ninja.
- Tests run with `filterwarnings = ["error"]`, so a ninja `UserWarning`, for
  example about a missing path parameter, fails the suite.
- black and flake8 must pass. Match the existing docstring and comment style
  in `scrudful/fastapi.py`.
- All existing tests must pass **unchanged**.
- Validators and precondition results come only from what `load` returned or
  raised, or from the queryset a list handler returned. Never from a separate
  lookup (§ "The rule everything follows").
- The adapter never decides policy, such as PUT-create. `load` returning
  `None` follows `evaluate_preconditions` exactly (§1).
- Default Link rel and type values come from `scrudful/_adapter.py`:
  - `DEFAULT_SCHEMA_REL` is `"describedBy"`;
  - `DEFAULT_SCHEMA_TYPE` is `"application/json"`;
  - `DEFAULT_CONTEXT_REL` is `"http://www.w3.org/ns/json-ld#context"`;
  - `DEFAULT_CONTEXT_TYPE` is `"application/ld+json"`.
- Release: the MR sets the version to **0.4.0** (`uv version 0.4.0 --no-sync`).
  The `version-check` CI job enforces this.

## Review Focus

1. **The endpoint module uses `from __future__ import annotations`.** Hidden
   parameters (the `load_as` object, `ScrudfulResponse`, `HttpResponse`) are
   still recognised and hidden from ninja. Test: Task 3, Step 1
   (`test_future_annotations_module`).
2. **The handler raises after `load` succeeds** (e.g. `HttpError(403)`). The
   client gets ninja's error response, with no `ETag` or `Last-Modified`.
   Test: Task 3 (`test_handler_error_has_no_validators`).
3. **The router is mounted under API-level auth, with a nested router and a
   prefix.** OPTIONS still exists and needs no auth, and the other operations
   still require it. Test: Task 8 (`test_options_nested_router_api_auth`).
4. **Bad pagination input** (`page=0`, `page=x`, `page_size=5000`). The
   client gets a 422 from ninja's validation, never a 500. A page past the end
   gets 404. Test: Task 7 (`test_pagination_bad_input`).
5. **A path where only some operations are scrudful.** OPTIONS describes every
   method the path serves. Non-scrudful methods get no links and no
   precondition parameters, as in DRF. Test: Task 8
   (`test_options_mixed_operations`).

---

### Task 1: Shared Django helpers, packaging

**Files:**
- Create: `scrudful/_django.py`
- Modify: `scrudful/rest_framework.py`. Remove `_precondition_headers` and
  replace the plain-Django branch of `_conditional_response`, plus the
  naive-datetime handling in `ScrudfulViewFunc._validators` (around lines
  275–280), with calls into `_django`.
- Modify: `pyproject.toml`:
  - add the extra `ninja = ["django>=4.2", "django-ninja>=1.6"]`;
  - change the `dev` group's `"scrudful[drf,fastapi]"` to
    `"scrudful[drf,fastapi,ninja]"`, and add `"jsonschema>=4.23"`.
- Modify: `uv.lock` (via `uv lock`)
- Test: `tests/test_django_helpers.py`

**Interfaces:**
- Produces (`scrudful/_django.py`):
  - `precondition_headers(request: HttpRequest) -> dict[str, str]`: the same
    behaviour as today's `rest_framework._precondition_headers`.
  - `conditional_response(result: ConditionalResult) -> HttpResponseBase`:
    `HttpResponseNotModified` with `result.headers` when `result.body is None`,
    otherwise `JsonResponse(result.body, status=result.status)` with
    `result.headers`. Then `expose_headers` is applied.
  - `aware(value: Any) -> Any`: a naive `datetime` is made aware in
    `timezone.get_default_timezone()`. Anything else is returned unchanged.

- [ ] **Step 1: Write failing tests** in `tests/test_django_helpers.py`:
  - `test_precondition_headers_reads_django_request`: a `RequestFactory` GET
    with `HTTP_IF_MATCH='"a"'` and a blank `HTTP_IF_NONE_MATCH=" "` returns
    `{"if-match": '"a"'}`.
  - `test_conditional_response_304`: given
    `ConditionalResult(304, {"ETag": '"a"'}, None)`, the status is 304,
    `ETag` is `'"a"'`, and `Access-Control-Expose-Headers` contains `ETag`.
  - `test_conditional_response_json`: given
    `ConditionalResult(412, {}, {"detail": "x"})`, the status is 412 and the
    JSON body is `{"detail": "x"}`.
  - `test_aware_uses_time_zone` (with `settings.TIME_ZONE="Europe/Paris"`
    via the pytest-django `settings` fixture): `aware(datetime(2026, 1, 1))`
    has `utcoffset() == timedelta(hours=1)`. An aware value and a float are
    returned unchanged.
- [ ] **Step 2: Run them.** `uv run pytest tests/test_django_helpers.py -q`
  → FAIL (`ModuleNotFoundError: scrudful._django`).
- [ ] **Step 3: Implement `scrudful/_django.py`, and make
  `rest_framework.py` call it.** The DRF branch (`drf=True` → DRF
  `Response`) stays in `rest_framework.py`.
- [ ] **Step 4: Update the packaging, then lock.** Edit `pyproject.toml` as
  listed above, then run `uv lock`. Expected: `uv.lock` gains
  `django-ninja 1.7.1`, `pydantic` and `jsonschema`.
- [ ] **Step 5: Run the whole suite.** `uv run pytest -q` → all pass,
  including every existing DRF test, unchanged.
- [ ] **Step 6: Commit.**
  `git add scrudful/_django.py scrudful/rest_framework.py pyproject.toml uv.lock tests/test_django_helpers.py && git commit -m "Move the Django request/response helpers into scrudful._django; add the ninja extra"`

### Task 2: Collection documents (`scrudful/collection.py`)

**Files:**
- Create: `scrudful/collection.py` (stdlib-only)
- Test: `tests/test_collection.py`

**Interfaces:**
- Produces:
  - `ENVELOPE_FIELDS = ("href", "etag", "last_modified", "link", "content")`
  - `PAGE_FIELDS = ("count", "page_count", "first", "previous", "next", "last", "content")`
  - `collection_schema(item_schema_uri: str, *, schema_id: str | None = None, title: str | None = None) -> dict[str, Any]`
    returns a JSON Schema (draft 2020-12, with `"$schema"` set) for the
    §5 envelope:
    - `count` and `page_count` are integers ≥ 0;
    - `first` and `last` are `uri` strings;
    - `previous` and `next` are `uri` strings or `null`;
    - `content` is an array of envelope objects (`href` uri, `etag` string,
      `last_modified` string, `link` string, and `content` as
      `{"$ref": item_schema_uri}`).

    All fields are required. scrud-django nested `type`/`items` under
    `properties`; don't copy that.
  - `collection_context(item_type_iri: str, *, vocabulary: str, collection_iri: str | None = None) -> dict[str, Any]`
    returns a JSON-LD context: `{"@context": {...}}` mapping each page and
    envelope field to `vocabulary + name`, with `content` as an `@list`
    container. `vocabulary` is **required** and has no default.

    Ruling: this is the "neutral default" for IRIs. Requiring the value
    dictates nothing, and no organisation's IRI is baked in.

- [ ] **Step 1: Write failing tests:**
  - `test_schema_is_valid_draft_2020_12`: `jsonschema.Draft202012Validator.check_schema(collection_schema("https://x/item.json"))`
    passes.
  - `test_schema_validates_envelope`: given a sample envelope with 1 item
    whose `content` is `{}`, a validator using a `referencing.Registry` that
    maps `https://x/item.json` to `{"type": "object"}` accepts it. Removing
    `href` from the item makes it reject.
  - `test_schema_previous_next_nullable`: `previous: None` is accepted, and
    `first: None` is rejected.
  - `test_context_maps_fields`: `collection_context("https://x/Role", vocabulary="https://v/#")["@context"]["count"] == "https://v/#count"`,
    and `["content"]["@container"] == "@list"`.
  - `test_context_requires_vocabulary`: calling `collection_context("i")`
    without `vocabulary` raises `TypeError`.
  - `test_stdlib_only`: the AST of `scrudful/collection.py` imports nothing
    outside the stdlib and `scrudful`. Copy the pattern of the existing
    stdlib-only test in `tests/test_adapter.py`, if there is one; otherwise
    walk the AST for `Import` and `ImportFrom` nodes.
- [ ] **Step 2: Run them.** `uv run pytest tests/test_collection.py -q` →
  FAIL.
- [ ] **Step 3: Implement both functions.**
- [ ] **Step 4: Run them.** `uv run pytest tests/test_collection.py -q` →
  PASS.
- [ ] **Step 5: Commit.**
  `git commit -m "Add scrudful.collection: JSON Schema and JSON-LD context of the scrud list envelope"`

### Task 3: `scrudful.ninja.scrudful` on single resources (sync)

**Files:**
- Create: `scrudful/ninja.py`
- Test: `tests/test_ninja_adapter.py`, `tests/ninja_future_annotations.py`
  (modelled on `tests/fastapi_future_annotations.py`)

**Interfaces:**
- Consumes: `scrudful._django.conditional_response`, `aware`, and
  `precondition_headers`. Also `_adapter.check_validators`, `validator_policy`,
  `set_validator_headers`, `expose_headers`, `READ_DESCRIPTIONS`,
  `WRITE_DESCRIPTIONS`, and the `DEFAULT_*` constants. From `conditional`:
  `evaluate_preconditions`, `quote_etag`, `http_timestamp`. From `utils`:
  `join_links`, `link_content`.
- Produces:
  ```python
  def scrudful(
      *,
      load: Callable[..., Any] | None = None,   # load(request, **params) -> obj | None; may raise
      load_as: str = "obj",                      # handler parameter receiving the loaded object
      etag_func: Callable[[Any], Any] | None = None,           # etag_func(obj)
      last_modified_func: Callable[[Any], Any] | None = None,  # last_modified_func(obj)
      schema_link_or_func: Any = None, schema_rel_or_func: Any = None, schema_type_or_func: Any = None,
      context_link_or_func: Any = None, context_rel_or_func: Any = None, context_type_or_func: Any = None,
      request: Entity | None = None,             # Task 5
      responses: Mapping[int, Entity] | None = None,  # Task 5
      error: Entity | None = None,               # Task 5
  ) -> Callable[[F], F]
  CONDITIONAL_READ_RESPONSES: dict[str, dict[str, str]]   # {"304": {"description": ...}, "400": ..., "412": ...}
  CONDITIONAL_WRITE_RESPONSES: dict[str, dict[str, str]]  # {"400": ..., "412": ..., "428": ...}
  ```
  - The constants are used as
    `openapi_extra={"responses": CONDITIONAL_READ_RESPONSES}`, with keys as
    strings, as OpenAPI requires. Build them from `READ_DESCRIPTIONS` and
    `WRITE_DESCRIPTIONS`.
  - A link value is a `str` or `func(request, obj)` (§4). It receives the
    loaded object, or `None` when nothing was loaded.
  - The decorated function carries `_scrudful: _Config`, a private dataclass
    holding everything above. Task 8 reads it.
  - `TypeError` at decoration time:
    - when `etag_func` or `last_modified_func` is set without `load` (§1);
    - when `load` is set but the handler has no parameter named `load_as`;
    - for a generator handler.

Behaviour, in this order (§1):
1. ninja calls the wrapper after auth and throttling.
2. The wrapper calls `load(request, **params)`, where `params` is ninja's
   validated keyword arguments minus the hidden ones.
3. The validators are called with the object; naive datetimes go through
   `_django.aware` and then `http_timestamp`.
4. `evaluate_preconditions(method, precondition_headers(request), etag=…, last_modified=…, require_precondition=True)`.
   An early result is returned as `conditional_response(result)`. Copy the
   temporal response's cookies onto it, and on a 304 also its other
   non-`Content-*` headers (the FastAPI `_conditional_response` rules).
5. The handler is called with the object passed as `load_as`.
6. After the handler, work out the status:
   - a returned `ninja.Status` → its `status_code`;
   - else, if the operation declares one response code, that code (captured
     through `ninja.utils.contribute_operation_callback`);
   - else 200.
7. `validator_policy(method, status)`. For PUT/PATCH the refresh **calls
   `load` again** and recomputes. Ruling: this always describes the
   representation just written.
8. Headers go on the temporal `HttpResponse`. If the handler returns an
   `HttpResponseBase`, they go on that instead. The wrapper sets
   `ETag`/`Last-Modified` with `set_validator_headers`, then `Link` joined
   with any existing value, then `expose_headers`.

**Signature mechanics:**
- The wrapper's `__signature__` is the handler's signature without:
  - the `load_as` parameter;
  - any parameter annotated `ScrudfulResponse` (Task 5).
- A keyword-only parameter annotated `django.http.HttpResponse`, with a
  private name, is added when the handler has none. ninja detects it by
  `annotation is HttpResponse`.
- Resolve string annotations as `scrudful/fastapi.py` `_signature` and
  `_annotation_is` do, and reuse that logic if it can be shared without a
  FastAPI import.

- [ ] **Step 1: Write failing tests** (sync). Use `ninja.testing.TestClient`,
  with an in-memory `ITEMS` dict for `load`, which raises `HttpError(404)`
  for missing ids.
  - `test_get_sets_validators_and_links`: a 200 response has
    `ETag: "v1"`, a `Last-Modified` HTTP-date, and
    `Link: </schemas/item>; rel="describedBy"; type="application/json", </contexts/item>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"`.
    `Access-Control-Expose-Headers` includes `ETag` and `Link`.
  - `test_get_if_none_match_304`: the response is 304 with the `ETag`, and
    the handler is not called (assert this with a call counter).
  - `test_put_without_precondition_428`, `test_put_stale_if_match_412`,
    `test_malformed_if_match_400`.
  - `test_put_success_fresh_validators`: the handler bumps the version, and
    the response `ETag` is `"v2"`.
  - `test_unauthenticated_get_401_before_load`: build
    `NinjaAPI(auth=APIKeyHeader subclass)`. No credentials plus
    `If-None-Match` matching the ETag → 401, and `load` is never called.
  - `test_unauthenticated_put_401`: a `PUT` with a stale `If-Match` and no
    credentials → 401 (not 412), and without `If-Match` → 401 (not 428).
    These are issue #1's table.
  - `test_load_http404_uses_ninja_handler`: the response is a 404 with ninja's
    JSON body (`{"detail": "Not Found"}`), not Django's HTML page.
  - `test_hidden_resource_same_for_all_if_headers`: `load` raises 404. For
    GET, PUT, PATCH and DELETE, each combined with no header, `If-Match: *`,
    `If-None-Match: *` and a stale `If-Match`, the response is 404 with no
    `ETag`.
  - `test_load_none_follows_rfc`: `load` returns `None`. PUT with
    `If-None-Match: *` reaches the handler. PUT with `If-Match: "x"` → 412.
    GET reaches the handler, which returns `HttpError(404)`.
  - `test_handler_error_has_no_validators` (Review Focus 2).
  - `test_load_patterns[...]`: one case per row of the spec's
    "Choosing what `load` returns" table:
    - **No PUT-create:** missing and hidden both raise 404, so every method
      and `If-*` combination gives an identical 404.
    - **PUT-create, nothing hidden:** `If-None-Match: *` creates, and
      `If-Match` gives 412.
    - **PUT-create with hidden rows:** `If-Match` gives 412 for missing and
      404 for hidden. The test documents that difference.
    - **Hidden treated as missing:** the handler sees a create that collides
      with a hidden row and returns 409, and the hidden row is unchanged.
  - `test_early_response_keeps_cookies`: an `auth` callable sets a cookie on
    the temporal response, so a 412 carries the `Set-Cookie`. A 304 also
    keeps a `Cache-Control` the handler-side code set, but not `Content-*`
    headers.
  - `test_head_routed_with_get`:
    `@router.api_operation(["GET", "HEAD"], ...)`. HEAD gives 200 with
    `ETag`, and a matching `If-None-Match` gives 304.
  - `test_params_are_validated_types`: `load` receives `pk` as an `int` for
    `pk: int`.
  - `test_hidden_params_not_in_openapi`: the `load_as` parameter and the
    temporal-response parameter are absent from
    `api.get_openapi_schema()["paths"][...]["get"]["parameters"]`.
  - `test_openapi_constants`:
    `openapi_extra={"responses": CONDITIONAL_WRITE_RESPONSES}` puts `428` in
    the schema's PUT responses.
  - `test_decoration_errors`: the three `TypeError` cases above.
  - `test_future_annotations_module` (Review Focus 1): the endpoints are
    defined in `tests/ninja_future_annotations.py`, and GET gives 200 with
    `ETag`.
- [ ] **Step 2: Run them.** `uv run pytest tests/test_ninja_adapter.py -q`
  → FAIL (no module).
- [ ] **Step 3: Implement `scrudful(...)` and the constants in
  `scrudful/ninja.py`.** The module docstring follows `scrudful/fastapi.py`.
- [ ] **Step 4: Run them.** `uv run pytest tests/test_ninja_adapter.py -q`
  → PASS. Then run `uv run pytest -q` → all pass.
- [ ] **Step 5: Commit.**
  `git commit -m "Add scrudful.ninja: conditional requests and Link headers for ninja operations"`

### Task 4: Async operations

**Files:**
- Modify: `scrudful/ninja.py`
- Test: `tests/test_ninja_async.py`

**Interfaces:**
- Consumes: Task 3's `scrudful`.
- Produces: no new names. When the handler is `async def`, the wrapper is
  `async def`, so ninja builds an `AsyncOperation`. `load`, the validators and
  the link callables may each be sync or `async def`, and an awaitable result
  is awaited. Inside an async wrapper, sync callables run through
  `asgiref.sync.sync_to_async(func, thread_sensitive=True)`.

- [ ] **Step 1: Write failing tests** with `ninja.testing.TestAsyncClient`
  and `pytest.mark.asyncio`-free style. Use `asgiref.sync.async_to_sync`
  around the client calls, so no new test plugin is needed.
  - Parametrise Task 3's core cases (200 validators, 304, 428, 412, fresh
    validators, 401-before-load, hidden 404) over `load` being sync or async.
    The handler is always async. Name: `test_async_matches_sync[...]`.
  - `test_sync_load_runs_in_thread`: a sync `load` that touches the ORM
    (`Item.objects.count()` from the example DRF app, with the `db` fixture)
    raises no `SynchronousOnlyOperation`.
- [ ] **Step 2: Run them.** `uv run pytest tests/test_ninja_async.py -q` →
  FAIL.
- [ ] **Step 3: Implement the async wrapper path.** Share the logic with the
  sync path (one implementation parametrised by a call helper, as
  `scrudful/fastapi.py` `_call` does), not a copy.
- [ ] **Step 4: Run them.** `uv run pytest tests/test_ninja_async.py tests/test_ninja_adapter.py -q`
  → PASS.
- [ ] **Step 5: Commit.**
  `git commit -m "scrudful.ninja: support async operations"`

### Task 5: Entities, the runtime `ScrudfulResponse`, general POST, error Links

**Files:**
- Modify: `scrudful/ninja.py`
- Test: `tests/test_ninja_entities.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Entity:
      schema_link_or_func: Any = None
      schema_rel_or_func: Any = None
      schema_type_or_func: Any = None
      context_link_or_func: Any = None
      context_rel_or_func: Any = None
      context_type_or_func: Any = None

  class ScrudfulResponse:      # per-request; injected into a handler parameter annotated ScrudfulResponse
      status: int | None       # None: ninja's normal status rules
      location: str | None
      etag: Any                # raw value; quote_etag applied; overrides computed
      last_modified: Any       # datetime | float; overrides computed
      entity: Entity | None    # overrides the declared entity for this response
      created: Any             # object for create validators/Location (Task 6); default: unset
  ```
  - The six keyword arguments `schema_*`/`context_*` on `scrudful(...)` are
    the **default 2xx response entity**.
  - `responses={status: Entity}` overrides it per status, including non-2xx
    statuses the handler returns.
  - `request=Entity(...)` describes the request body. It is used by OPTIONS
    (Task 8) and puts no header on the response.
  - `error=Entity(...)` adds Links to scrudful's own 400, 412 and 428 bodies
    (§7). Without it, nothing changes.
  - The Link for a response is resolved in this order:
    1. `ScrudfulResponse.entity`;
    2. `responses[status]`;
    3. the default entity, for 2xx only;
    4. none.

    Headers the handler set directly are never overwritten. `Link` is
    appended to.
  - When `ScrudfulResponse.status` is set and the handler did not return a
    `Status`, the wrapper returns `ninja.Status(status, result)`.

- [ ] **Step 1: Write failing tests:**
  - `test_general_post_200_result_links`:
    `POST /items/search` declares `responses={200: Entity("/schemas/results", context_link_or_func="/contexts/results")}`
    and `request=Entity("/schemas/query", ...)`. The response is 200 with
    the results Links, and with no `ETag` and no `Location`.
  - `test_per_status_entity`: the handler returns `Status(404, {...})` with
    `responses={404: Entity("/schemas/error")}`. The response has
    `Link: </schemas/error>; rel="describedBy"; …`.
  - `test_default_entity_only_on_2xx`: a 404 without a `responses` entry
    gets no Link.
  - `test_runtime_overrides`: the handler sets `meta.status = 202`,
    `meta.location = "/jobs/1"`, `meta.etag = "j1"` and
    `meta.entity = Entity("/schemas/job")`. The response is 202 with
    `Location: /jobs/1`, `ETag: "j1"` and the job Link, and
    `Access-Control-Expose-Headers` includes `Location`.
  - `test_scrudful_response_hidden_from_openapi`.
  - `test_error_links_on_412_428_400`: with `error=Entity("/schemas/problem")`,
    each early error carries that Link. Without it, there is no Link (the
    existing behaviour).
  - `test_post_action_on_item_preconditions_not_required`: `POST /items/1/approve`
    with `load` and `etag_func`. No `If-Match` → the handler runs (never
    428). A stale `If-Match` → 412.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_ninja_*.py -q` → PASS.
- [ ] **Step 5: Commit.**
  `git commit -m "scrudful.ninja: request/response entities, ScrudfulResponse, error Links"`

### Task 6: `scrudful_create` (§2)

**Files:**
- Modify: `scrudful/ninja.py`
- Test: `tests/test_ninja_create.py`

**Interfaces:**
- Consumes: Task 5's `Entity` and `ScrudfulResponse`.
- Produces:
  ```python
  def scrudful_create(
      *,
      location_func: Callable[[Any, Any], str],   # location_func(request, obj)
      etag_func=None, last_modified_func=None,     # item validators, called with the created object
      schema_link_or_func=None, schema_rel_or_func=None, schema_type_or_func=None,
      context_link_or_func=None, context_rel_or_func=None, context_type_or_func=None,  # item entity
      request: Entity | None = None,   # default: the item entity (as DRF's create does)
      responses: Mapping[int, Entity] | None = None,
      error: Entity | None = None,
  ) -> Callable[[F], F]
  ```
  - This is a preset over Task 5's machinery: the default status is 201, there
    is no `load` and no precondition evaluation, and the created object is
    `ScrudfulResponse.created` if the handler set it, otherwise the handler's
    return value (unwrapped from `Status`).
  - On a 201 response the wrapper sets:
    - `Location` (from `location_func`);
    - the item Links (link callables get `obj` = the created object);
    - `ETag` and `Last-Modified` from the item validators.
  - A non-201 result (e.g. `Status(400, ...)`) gets none of these.

- [ ] **Step 1: Write failing tests:**
  - `test_create_201_location_links_validators`: `POST /items` gives 201,
    with `Location: /items/3`, the item Links, `ETag: "v1"`, a
    `Last-Modified`, and an expose header listing `ETag`, `Link` and
    `Location`.
  - `test_create_validators_match_detail_get`: the `ETag` from the 201 equals
    the `ETag` of a following `GET /items/3`. The detail GET uses the same
    `etag_func`.
  - `test_create_uses_meta_created`: the handler returns a dict and sets
    `meta.created = item`, and the validators come from `item`.
  - `test_create_never_428` and `test_create_non_201_no_location`.
  - `test_create_unauthenticated_401`.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_ninja_*.py -q` → PASS.
- [ ] **Step 5: Commit.**
  `git commit -m "scrudful.ninja: scrudful_create (201, Location, item validators)"`

### Task 7: `scrudful_list` — the envelope and pagination (§5)

**Files:**
- Modify: `scrudful/ninja.py`
- Test: `tests/test_ninja_list.py`

**Interfaces:**
- Consumes: `scrudful.collection.ENVELOPE_FIELDS` and `PAGE_FIELDS`, for the
  field names. Also Task 3's link and validator helpers.
- Produces:
  ```python
  def scrudful_list(
      *,
      href_func: Callable[[Any, Any], str],          # href_func(request, item); absolute URL recommended
      etag_func=None, last_modified_func=None,       # item validators (same callables as the detail op)
      schema_link_or_func=None, schema_rel_or_func=None, schema_type_or_func=None,
      context_link_or_func=None, context_rel_or_func=None, context_type_or_func=None,  # item entity -> envelope "link"
      list_etag_func=None, list_last_modified_func=None,   # called with the queryset; optional
      list_schema_link_or_func=None, list_schema_rel_or_func=None, list_schema_type_or_func=None,
      list_context_link_or_func=None, list_context_rel_or_func=None, list_context_type_or_func=None,
      page_size: int = 100,
      max_page_size: int = 1000,
      error: Entity | None = None,
  ) -> Callable[[F], F]
  ```
  - The handler returns a queryset or a sequence, possibly wrapped in
    `Status`. The operation must declare `response=List[ItemOut]`.
    Otherwise `ninja.errors.ConfigError` is raised when the operation is
    built, as ninja's `@paginate` does.
  - The `ninja.utils.contribute_operation_callback` callback replaces that
    response model with a generated pydantic model named
    `Scrudful<Item>Page`. Its fields are `count: int`, `page_count: int`,
    `first: str`, `previous: str | None`, `next: str | None`, `last: str`,
    and `content: list[Scrudful<Item>Envelope]`. The envelope's fields are
    `href: str`, `etag: str | None`, `last_modified: str | None`,
    `link: str | None` and `content: Item`.
  - `ninja.utils.contribute_operation_args` adds a query-parameter schema
    named `scrudful_pagination`: `page: int = Field(1, ge=1)` and
    `page_size: int = Field(page_size, ge=1, le=max_page_size)`.
  - Page counting:
    - `count` is `qs.count()` for a `QuerySet`, otherwise `len()`;
    - `page_count` is `ceil(count / page_size)`;
    - a page greater than `max(page_count, 1)` raises `HttpError(404)`.
      Ruling: matches DRF's `PageNumberPagination`.
  - Page URLs (`first`, `last`, `previous`, `next`) are
    `request.build_absolute_uri()` with `page` replaced and other query
    parameters kept. `previous` and `next` are `None` at the ends, and
    `first` and `last` are page 1 when the collection is empty.
  - The envelope's `etag` is `quote_etag(etag_func(item))`, its
    `last_modified` is the HTTP-date, and its `link` is the item's Link value
    (`join_links` of the item schema and context).
  - Response headers carry the `list_*` Links. With `list_*` validators set,
    the evaluation runs **after** the handler returns the queryset, and a GET
    can get 304.
  - Async handlers work, with the queryset evaluated through `sync_to_async`.

- [ ] **Step 1: Write failing tests.** These use the `items.Item` model of
  the DRF example (the `db` fixture) for the queryset cases, and a list for
  the sequence case.
  - `test_list_envelope_shape`: 3 items with `page_size=2` →
    `count == 3`, `page_count == 2`, `previous is None`, and `next` ends with
    `?page=2&page_size=2`. Each envelope has the five fields, its `etag`
    equals the detail GET's `ETag`, and its `link` equals the detail GET's
    `Link`.
  - `test_list_scoped_by_handler_queryset`: the handler filters out item 2,
    so `count == 2` and no `href` mentions `/2`.
  - `test_list_headers`: the response `Link` is the `list_*` Link, and there
    is no `ETag` unless `list_etag_func` is set.
  - `test_list_validators_304`: with `list_etag_func=lambda qs: f"n{qs.count()}"`,
    `If-None-Match: "n3"` gives 304.
  - `test_list_openapi_is_envelope`: the OpenAPI 200 schema `$ref`s
    `ScrudfulItemOutPage`, and the `page` and `page_size` query parameters
    are listed.
  - `test_list_requires_collection_response`: `response=ItemOut` raises
    `ConfigError` when the operation is built.
  - `test_pagination_bad_input` (Review Focus 4): `page=0`, `page=x` and
    `page_size=5000` each give 422. `page=9` gives 404.
  - `test_list_async_handler`.
  - `test_list_unauthenticated_401`.
  - `test_envelope_validates_against_collection_schema`:
    `jsonschema` validates the body against
    `collection_schema("https://x/item.json")`, with the item `$ref` resolved
    to `{"type": "object"}`.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_ninja_*.py tests/test_collection.py -q`
  → PASS.
- [ ] **Step 5: Commit.**
  `git commit -m "scrudful.ninja: scrudful_list (scrud envelope, pagination, collection Links)"`

### Task 8: OPTIONS — the shared description builder and `ScrudfulRouter` (§6)

**Files:**
- Modify: `scrudful/_adapter.py`. Add the pure description functions.
- Modify: `scrudful/rest_framework.py`. `ScrudfulMetadata` calls them, with no
  output change.
- Modify: `scrudful/ninja.py`
- Test: `tests/test_ninja_options.py`. Also add `tests/test_adapter.py` cases
  for the new functions.

**Interfaces:**
- Produces (`scrudful/_adapter.py`, stdlib-only, pure). These are lifted from
  `ScrudfulMetadata`, and their output equals what it produces today:
  - `describe_read(content: dict | None, conditional: bool) -> dict`, which is
    `determine_metadata_for_get`'s body.
  - `describe_write(method: str, content: dict | None, conditional: bool, has_etag: bool, has_last_modified: bool) -> dict`.
    `method` is one of `post`, `put`, `patch`, `delete`. It covers today's
    `_write` with its body descriptions and statuses (`201`, `200`, `200`,
    `204`) and `_STATUS_TEXT`.
  - `conditional_header_parameters(has_etag: bool, has_last_modified: bool) -> list[dict] | None`,
    which is today's `required_conditional_headers`.
- Produces (`scrudful/ninja.py`):
  ```python
  class ScrudfulRouter(ninja.Router):
      def __init__(self, *args, options_schema_link: str | None = None,
                   options_context_link: str | None = None, **kwargs): ...
  ```
  - It overrides `add_api_operation`, following the pattern of ninja's
    `RouterPaginated`.
  - After `super()`, it adds the OPTIONS operation for the path. This happens
    when the new operation's `view_func` has `_scrudful` and the path has no
    OPTIONS operation yet. The call is
    `super().add_api_operation(path, ["OPTIONS"], view, auth=None, include_in_schema=False)`.
  - The OPTIONS view declares each path parameter
    (`ninja.signature.utils.get_path_param_names(path)`) as `str`, so ninja
    emits no missing-parameter warning.
  - When the implementer later adds their own operation whose methods include
    `"OPTIONS"`, the automatic one is removed from that path's
    `PathView.operations`. A pre-existing user OPTIONS prevents adding it.
  - The OPTIONS body is built at request time from the path's operations.
    For each method in (`get`, `post`, `put`, `patch`, `delete`) that the
    path serves, plus `head` when an operation routes it (described like
    `get`; DRF never lists head, so this is part of the superset):
    - start from `describe_read` / `describe_write`, using the scrudful
      config's entity links, called with `(request, None)`. Methods that
      aren't scrudful get `content=None` and `conditional=False`, as in DRF;
    - add response `content` from the `responses` entities and the default
      2xx entity;
    - add `requestBody` content from `request=` (create: the item entity);
    - add any extra status codes the operation declares (from
      `op.response_models`), with `http.HTTPStatus(code).phrase`.
  - The response is `JsonResponse(body)`, with `Link` set to the
    `options_schema_link` / `options_context_link` Links when configured
    (omitted otherwise), and with `expose_headers`.
  - Users of `NinjaAPI` pass `NinjaAPI(default_router=ScrudfulRouter(...))`.

- [ ] **Step 1: Write failing tests:**
  - `test_adapter_describe_matches_drf` (in `tests/test_adapter.py`): for
    each method, the old `ScrudfulMetadata` expectations from
    `tests/test_rest_framework.py` are reproduced by the new functions. This
    test plus the unchanged DRF suite is the no-change proof.
  - `test_options_auto_added`: on a `ScrudfulRouter` with GET/PUT/DELETE at
    `/items/{pk}`, `OPTIONS /items/1` gives 200, with body keys
    `{"get", "put", "delete"}`. PUT has `parameters` naming `If-Match`, and
    `requestBody.content["application/json"]` is
    `{"schema": "/schemas/item", "context": "/contexts/item"}`.
  - `test_options_no_auth_no_load`: under API-level auth with no
    credentials, the response is 200, and `load` is never called.
  - `test_options_same_for_missing_and_hidden`: `OPTIONS /items/999` gives a
    body identical to `OPTIONS /items/1`.
  - `test_options_links_configured_and_omitted`.
  - `test_options_superset_of_drf`: an equivalent DRF viewset, decorated with
    `scrudful_viewset` and the same links and validators, has an OPTIONS body
    D; the ninja body is N. Every key path in D exists in N with an equal
    value.
  - `test_user_options_wins`: the user's OPTIONS is declared after the
    scrudful operation, and the user's handler answers.
  - `test_options_mixed_operations` (Review Focus 5).
  - `test_options_nested_router_api_auth` (Review Focus 3): a
    `ScrudfulRouter` nested in another router under `NinjaAPI(auth=...)`
    with a prefix. OPTIONS gives 200 without credentials, and GET gives 401.
  - `test_cors_preflight_not_broken`: OPTIONS with `Origin` and
    `Access-Control-Request-Method: PUT` still gives 200 with the
    description.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement the `_adapter.py` functions and switch
  `ScrudfulMetadata` to them.** Then run `uv run pytest tests/test_rest_framework.py tests/test_adapter.py -q`
  → PASS, unchanged.
- [ ] **Step 4: Implement `ScrudfulRouter` and the OPTIONS body.**
- [ ] **Step 5: Run** `uv run pytest -q` → all pass.
- [ ] **Step 6: Commit.**
  `git commit -m "scrudful.ninja: automatic OPTIONS (HTTP Schema Resource) via ScrudfulRouter; share the description builder with DRF"`

### Task 9: Example, docs, release metadata, version-floor and no-DRF checks

**Files:**
- Create: `examples/ninja/`, a Django project `scrudful_ninja` (settings,
  urls) with a `roles` app.
  - `Role` model fields: `name`, `rbac_context`, `etag`, `modified_at`.
  - `load` is scoped by `rbac_context` from a header, to stand in for
    scoped-rbac.
  - It covers detail GET/PUT/PATCH/DELETE, create, `POST /roles/search`,
    the list, OPTIONS, error Links, and an async operation.
  - Include `examples/ninja/tests/test_example.py`.
- Create: `tests/examples/ninja/test_example_project.py`. It runs
  `sys.executable -m pytest examples/ninja/tests -q -p no:randomly --ds=scrudful_ninja.settings`
  in a subprocess, with `PYTHONPATH=examples/ninja`, and asserts return code
  0. The example needs its own settings, and the main suite's
  `DJANGO_SETTINGS_MODULE` is the DRF example's.
- Create: `tests/test_ninja_no_drf.py`. A subprocess runs
  `python -c "import sys; sys.modules['rest_framework'] = None; import scrudful.ninja"`
  and gets return code 0.
- Create: `docs/source/ninja.rst`, and add it to the `docs/source/index.rst`
  toctree. It must contain:
  - the "Choosing what `load` returns" table (all four patterns, verbatim
    from the spec, §1);
  - the CORS-preflight note (§6);
  - `default_router=ScrudfulRouter(...)` usage.
- Modify: `README.rst` (a "django-ninja" section after the FastAPI one) and
  `CHANGELOG.rst` (a new top section "0.4.0 (since 0.3.0)").
- Modify: `pyproject.toml`:
  - set `description` to `"HTTP conditional-request and Link-header helpers for SCRUDful REST APIs (Django REST Framework, django-ninja, FastAPI)"`;
  - add `"Framework :: Django :: 5.2"` only if a ninja trove classifier
    doesn't exist. Check `uv run --no-project --with trove-classifiers python -c "import trove_classifiers as t; print([c for c in t.classifiers if 'ninja' in c.lower()])"`
    and add any `django-ninja` classifier found.
- Version: run `uv version 0.4.0 --no-sync`, which updates `pyproject.toml`
  and `uv.lock`.

- [ ] **Step 1: Write the example's tests.** `examples/ninja/tests/test_example.py`
  has a test for each feature listed above. Then write the two wrapper tests
  (`test_example_project.py`, `test_ninja_no_drf.py`).
- [ ] **Step 2: Run them** → FAIL (no example).
- [ ] **Step 3: Build the example**, then run `uv run pytest -q` → all pass.
- [ ] **Step 4: Write the docs**, then build them: `uv run --group docs make -C docs html BUILDDIR=build SPHINXOPTS="-W --keep-going"`
  → no warnings.
- [ ] **Step 5: Check the version floor.** Run `uv run --with django-ninja==1.6.0 pytest tests/test_ninja_*.py -q -p no:randomly`
  → PASS. If it fails, raise the floor in the `ninja` extra to the lowest
  1.6.x or 1.7.x that passes, re-run `uv lock`, and note the reason in the
  CHANGELOG.
- [ ] **Step 6: Bump the version and check.** Run `uv version 0.4.0 --no-sync`,
  then `uv lock --check`, then `uv run --no-project --with packaging python scripts/release.py check-bump origin/master origin/master`.
  The last command is expected to print "Version 0.4.0 will be published
  when this is merged.", assuming `origin/master` is fetched and still at
  0.3.0.
- [ ] **Step 7: Run black and flake8.** Run `uv run black --check scrudful tests examples`
  and `uv run flake8 scrudful tests examples` → clean.
- [ ] **Step 8: Commit.**
  `git commit -m "scrudful.ninja: example project, docs, 0.4.0"`

## After the branch is reviewed (not a code task)

With the owner's go-ahead at that time, and not before:
- Open the follow-up issues in `dcharbon/scrudful`, one each for DRF and
  FastAPI, covering:
  - 201 validators;
  - request/response entities;
  - error Links;
  - load-based validators.
- Add the `RoleAssignmentViewSet` comment to `dcharbon/django-scoped-rbac`
  #14.
