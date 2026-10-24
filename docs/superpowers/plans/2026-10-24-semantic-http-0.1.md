# Semantic HTTP 0.1 Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every response scrudful generates conformant with Semantic HTTP 0.1 by default, and give implementers one documented helper for the responses the framework generates (issue #4).

**Architecture:**
- **Shared core.** The 0.1 vocabulary and policy go into the framework-neutral core (`scrudful/_adapter.py`, `scrudful/utils.py`): link defaults, Media Type Objects, the `options` entry, `Allow`, link de-duplication and absolute-URI resolution.
- **Entity.** A shared `Entity` dataclass (new `scrudful/entity.py`, re-exported by every adapter) carries a media type and becomes the way DRF and FastAPI declare error Links.
- **Adapters.** Each adapter then switches its OPTIONS document and Link headers to the core. FastAPI gains an OPTIONS helper.
- **Wire tests.** Per-adapter wire-conformance tests against the example apps are the acceptance gate.

**Tech Stack:** Python ≥ 3.10, Django ≥ 4.2, DRF ≥ 3.15, django-ninja 1.7.x, FastAPI ≥ 0.100; tests use pytest, pytest-django, jsonschema, pyld (all already in the `dev` group).

**Spec:** `docs/superpowers/specs/2026-10-24-semantic-http-0.1-conformance.md`. The normative text is the semantic-http spec at `~/dev/semantic-http-spec/source/*.rst`, with its normative sentences listed in `tests/fixtures/normative-statements.txt`.

## Global Constraints

- `scrudful/_adapter.py`, `scrudful/utils.py` and the new `scrudful/entity.py` stay stdlib-only. The package keeps no required dependencies.
- Exact values (define them once, in `scrudful/_adapter.py`; import them everywhere else):
  - `SCHEMA_REL = "describedby"`
  - `SCHEMA_TYPE = "application/schema+json"`
  - `JSON_LD_CONTEXT_REL = "http://www.w3.org/ns/json-ld#context"`
  - `CONTEXT_TYPE = "application/ld+json"`
  - `RDF_CONTEXT_REL = "https://w3id.org/semantic-http/ns#rdf-context"`
  - `HSR_SCHEMA_URL = "https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json"`
  - `HSR_CONTEXT_URL = "https://w3id.org/semantic-http/0.1/http-schema-resource.context.jsonld"`
  - `IN_BAND_MEDIA_TYPES = frozenset({"application/ld+json", "text/turtle", "application/n-triples", "application/n-quads", "application/trig", "application/rdf+xml"})`
  - `METHOD_KEYS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")`
- `DEFAULT_SCHEMA_REL`, `DEFAULT_SCHEMA_TYPE`, `DEFAULT_CONTEXT_REL` and `DEFAULT_CONTEXT_TYPE` remain as aliases of `SCHEMA_REL`, `SCHEMA_TYPE`, `JSON_LD_CONTEXT_REL` and `CONTEXT_TYPE`, so existing imports keep working.
- **JSON content** is a media type whose type/subtype is `application/json` or ends in `+json`, other than `application/ld+json` (`rdf_context.rst`). Media types compare by lowercase type/subtype with parameters ignored.
- **Media Type Object** (0.1, `http_schema_resource.rst`):
  - a JSON Schema gives `{"schema": {"$ref": <absolute>}}`;
  - any other schema type gives `{"x-schema": {"href": <absolute>, "type": <schema type>}}`;
  - a non-in-band media type also gets `"x-rdf-context": {"href": <absolute>, "type": <context type>}`.
  
  A schema is a JSON Schema exactly when its schema type is `application/schema+json`.
- Every URI in an OPTIONS body, and every Link target scrudful sends, is absolute. scrudful resolves relative values against the request's absolute URI:
  - Django: `request.build_absolute_uri()`;
  - Starlette: `str(request.url)`.
- **Headers the view sets itself are never overwritten** (existing rule). A Link the view set that already has a `describedby` relation (compared case-insensitively) suppresses scrudful's schema link. One with `JSON_LD_CONTEXT_REL` or `RDF_CONTEXT_REL` suppresses scrudful's context link. A view Link with another relation (e.g. `rel="next"`) suppresses nothing.
- **OPTIONS response** (non-preflight):
  - status 200, `Content-Type: application/json`;
  - Link `<HSR_SCHEMA_URL>; rel="describedby"; type="application/schema+json"`, then `<HSR_CONTEXT_URL>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"`;
  - `Allow` equal to the body's method keys, upper-cased, in body order, joined by `", "`;
  - the body has an `options` key (see `describe_options` in Task 1);
  - a `head` entry, when present, has no `content`.
- Tests run with `filterwarnings = ["error"]`. Code passes `uv run black --check .` and `uv run flake8` (max line 88).
- Commit messages follow the repo's style, `<area>: <summary>`, e.g. `scrudful.ninja: …`, `core: …`, `docs: …`, `tests: …`.
- Existing behaviour not named in a task is unchanged: preconditions, validators, the 304 rules, async support and expose headers.

## Review Focus

1. **A view Link of another relation, or with commas inside quoted parameters or URIs** (`<https://x/a,b>; rel="next"; title="a, b"`): scrudful's links are still added. Only same-kind relations suppress them. Pinned in Task 1 (`test_link_relations_*`, `test_merge_links_*`).
2. **Relative, network-path, query or fragment link values, and non-`str` values** (`"/s"`, `"//cdn/s"`, `"s?v=1"`, `"#frag"`, a lazy string, a Starlette `URL`): each resolves to the URI a browser would resolve it to, and absolute values are unchanged. Pinned in Task 1 (`test_resolve_uri_*`).
3. **Media types with parameters or upper case** (`Application/JSON; charset=utf-8`): the type/subtype becomes lowercase, the parameters are kept verbatim, and JSON/in-band detection ignores parameters. Pinned in Tasks 1 and 2.
4. **OPTIONS with `Accept: text/html` on DRF** (browsable API): still `application/json` with the HSR Links. Pinned in Task 5.
5. **A path mixing scrudful and non-scrudful methods** (e.g. a plain ninja PUT or a DRF `@action` without links): `Allow` still equals the body's keys, and the non-scrudful method's entry has no `content` and no `requestBody`. Pinned in Tasks 3 and 5.

---

### Task 1: Core 0.1 vocabulary, link policy and test helpers

**Files:**
- Modify: `scrudful/_adapter.py`, `scrudful/utils.py`
- Create: `tests/semantic_http.py` (test helpers); `tests/fixtures/semantic-http-0.1/http-schema-resource.schema.json`, `tests/fixtures/semantic-http-0.1/http-schema-resource.context.jsonld` and `tests/fixtures/semantic-http-0.1/README.md` (attribution)
- Test: `tests/test_utils.py`, `tests/test_adapter.py`, `tests/test_semantic_http_helpers.py`. Also update the tests that pin the old defaults: grep `tests/` for `describedBy` and for `type="application/json"` on schema links.

**Interfaces:**
- Produces (in `scrudful/utils.py`):
  - `resolve_uri(url: object, base: str) -> str`: `urllib.parse.urljoin(base, str(url))`.
  - `link_relations(field_value: str | None) -> set[str]`: the lower-cased relation types of every link-value in an RFC 8288 Link field value. A `rel` may hold several space-separated types, and it may be a quoted string or a token. Commas inside `<…>` or quoted strings do not split link-values. A malformed value yields what could be parsed, and never raises.
- Produces (in `scrudful/_adapter.py`):
  - the constants in Global Constraints;
  - `media_type_key(media_type: str) -> str`: lowercase type/subtype, stripped; parameters (after the first `;`) kept as written. `ValueError` unless the type/subtype is `type/subtype` with non-empty parts.
  - `essence(media_type: str) -> str`: the lowercase type/subtype without parameters.
  - `is_json(media_type: str) -> bool` and `is_in_band(media_type: str) -> bool`, both by essence.
  - `context_defaults(media_type: str) -> tuple[str | None, str | None]`: the context link's default `(rel, type)`.
    - JSON gives `(JSON_LD_CONTEXT_REL, CONTEXT_TYPE)`.
    - In-band gives `(None, None)`: no context link.
    - Anything else gives `(RDF_CONTEXT_REL, None)`: the type must be given.
  - `merge_links(existing: str | None, schema: str | None, context: str | None) -> str | None`: `join_links(existing, schema, context)` after dropping `schema` if `existing` has `describedby`, and `context` if it has `JSON_LD_CONTEXT_REL` or `RDF_CONTEXT_REL`.
  - `describe_content(media_type: str, schema: str | None, schema_type: str, context: str | None, context_type: str | None) -> dict[str, Any] | None`: `{media_type_key(media_type): <Media Type Object>}`, or `None` when `schema` is falsy. `x-rdf-context` is present iff `context` is truthy and the media type is not in-band. Its `type` is `context_type`, else `CONTEXT_TYPE` for JSON. The caller passes already-absolute URIs.
  - `describe_read(content, conditional, statuses=None, error_content=None)`: as today, plus `error_content` on the 400 and 412 entries when `conditional`.
  - `describe_write(method, content, conditional, has_etag, has_last_modified, statuses=None, response_content=None, error_content=None)`. As today, except:
    - `requestBody` is omitted when `content` is falsy;
    - `response_content` goes on every success status except 204;
    - `error_content` goes on the 400/412/428 entries it adds.
  - `describe_options() -> dict[str, Any]`: `{"responses": {"200": {"description": "OK", "content": describe_content("application/json", HSR_SCHEMA_URL, SCHEMA_TYPE, HSR_CONTEXT_URL, CONTEXT_TYPE)}}}`, a fresh dict each call.
  - `allow_header(body: Mapping[str, Any]) -> str`: the keys of `body` that are in `METHOD_KEYS`, upper-cased, in body order, joined with `", "`.
  - `options_links() -> str`: the OPTIONS response's Link field value (Global Constraints).
- Produces (in `tests/semantic_http.py`, used by Tasks 3–9):
  - `validate_http_schema_resource(body: dict) -> None`. Validates `body` against the vendored schema with `jsonschema.Draft202012Validator`, then asserts the rules the schema can't express:
    - every `$ref` and `href` under `content` is absolute (has a scheme);
    - content keys have lowercase type/subtype;
    - there are no duplicate type/subtype keys in one `content` map.
  - `parse_links(value: str | None) -> list[tuple[str, dict[str, str]]]`: `(target, params)` with lower-cased parameter names.
  - `link_problems(method: str, status: int, headers: Mapping[str, str], has_content: bool, hsr: dict | None = None) -> list[str]`: the spec's per-response rules.
    - Every response with content: exactly one `describedby` with a `type`.
    - JSON content: exactly one JSON-LD context link with `type="application/ld+json"`. In-band content: none.
    - Non-preflight OPTIONS: the OPTIONS rules of Global Constraints.
    - With `hsr`: Agreement, i.e. the Links equal the `$ref`/`href` of the entry chosen by method → status (exact, else `NXX`, else `default`) → essence of `Content-Type`. HEAD uses the `get` entry.
    
    An empty list means conformant.

- [ ] **Step 1: Vendor the spec artifacts.** Copy `~/dev/semantic-http-spec/source/_extra/0.1/http-schema-resource.schema.json` and `.context.jsonld` byte-for-byte into `tests/fixtures/semantic-http-0.1/`. Add a README there naming the source, the spec version (0.1, commit `bcc338e`) and the CC BY 4.0 licence (copyright 2019 Quansight LLC; 2026 David Charboneau).
- [ ] **Step 2: Write failing tests** in `tests/test_utils.py` and `tests/test_adapter.py`:
  - `test_resolve_uri_relative`: `resolve_uri("/s", "http://h/a/b") == "http://h/s"`.
  - `test_resolve_uri_variants`:
    - `"//cdn/s"` → `"http://cdn/s"`;
    - `"s?v=1"` against `"http://h/a/b"` → `"http://h/a/s?v=1"`;
    - `"#f"` → `"http://h/a/b#f"`;
    - an absolute `"https://x/y"` is unchanged.
  - `test_resolve_uri_non_str`: an object whose `__str__` returns `"/s"` resolves like the string.
  - `test_link_relations_*`:
    - `'<a>; rel="describedBy"'` → `{"describedby"}`;
    - `'<a>; rel="next prev", <b>; rel=self'` → `{"next", "prev", "self"}`;
    - `'<https://x/a,b>; rel="next"; title="a, b"'` → `{"next"}`;
    - `None` → `set()`;
    - `'garbage'` → `set()`.
  - `test_merge_links_*`:
    - a view `describedby` drops scrudful's schema link but keeps its context link;
    - a view `rel="next"` keeps both;
    - a view link with `RDF_CONTEXT_REL` drops scrudful's context link;
    - all `None` → `None`.
  - `test_media_type_key`:
    - `"Application/JSON; charset=UTF-8"` → `"application/json; charset=UTF-8"`;
    - `"text"` raises `ValueError`.
  - `test_is_json`: true for `application/json`, `application/problem+json` and `application/json; charset=utf-8`; false for `application/ld+json` and `text/csv`.
  - `test_is_in_band`: true for each of the six in-band types, also upper-cased and with parameters.
  - `test_context_defaults`: the three cases above.
  - `test_describe_content_json_schema`:
    - `describe_content("application/json", "https://h/s", SCHEMA_TYPE, "https://h/c", None)` == `{"application/json": {"schema": {"$ref": "https://h/s"}, "x-rdf-context": {"href": "https://h/c", "type": "application/ld+json"}}}`.
  - `test_describe_content_x_schema`:
    - schema type `application/xml` gives `{"x-schema": {"href": …, "type": "application/xml"}}` and no `schema`.
  - `test_describe_content_in_band`:
    - `application/ld+json` with a context gives no `x-rdf-context`.
  - `test_describe_content_no_schema`:
    - `None` gives `None`.
  - `test_describe_write_omits_request_body_without_content`.
  - `test_describe_write_response_content`:
    - the 200 of a PUT, and the 201 of a POST, carry `response_content`;
    - a DELETE's 204 never does.
  - `test_describe_error_content`:
    - 400/412/428 carry `error_content`; a 304 never does.
  - `test_describe_options_valid`:
    - `validate_http_schema_resource({"options": describe_options()})` passes.
  - `test_allow_header`:
    - `{"get":…, "head":…, "options":…, "x-a": 1}` → `"GET, HEAD, OPTIONS"`.
  - `test_options_links`:
    - equals the exact string in Global Constraints.
  - `test_defaults_are_0_1`:
    - `DEFAULT_SCHEMA_REL == "describedby"` and `DEFAULT_SCHEMA_TYPE == "application/schema+json"`.
  
  In `tests/test_semantic_http_helpers.py`:
  - `test_validate_rejects_0_0_shape`:
    - `{"get": {"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": "https://h/s", "context": "https://h/c"}}}}}}` raises.
  - `test_validate_rejects_relative_ref`.
  - `test_link_problems_*`:
    - a JSON 200 with only a `describedBy` link reports the missing context;
    - an OPTIONS whose `Allow` lists a method that isn't a body key reports it;
    - the spec's `person` example (`~/dev/semantic-http-spec/source/examples/person.http-schema-resource.json`, copied into the fixtures dir) passes Agreement for its GET example response.
- [ ] **Step 3: Run them** — `uv run pytest tests/test_utils.py tests/test_adapter.py tests/test_semantic_http_helpers.py -q`. Expected: the new tests fail (ImportError or AssertionError).
- [ ] **Step 4: Implement** the interfaces above. `link_relations` is a small hand-written scanner (no regex over the whole value). It splits on top-level commas, tracking `<…>` and quoted strings.
- [ ] **Step 5: Update the default-pin tests across `tests/`** so that schema links now read `rel="describedby"; type="application/schema+json"`. The adapters still build 0.0 OPTIONS bodies until their own tasks, so leave OPTIONS body assertions alone.
- [ ] **Step 6: Run the whole suite** — `uv run pytest -q`. Expected: all pass. Also `uv run black --check . && uv run flake8`.
- [ ] **Step 7: Commit** — `core: Semantic HTTP 0.1 link defaults, Media Type Objects and link policy`.

### Task 2: Shared `Entity` with a media type

**Files:**
- Create: `scrudful/entity.py`
- Modify: `scrudful/ninja.py` (remove the `Entity` class, import it from `scrudful.entity`, keep it in `__all__`; `_links`/`_link` take their defaults from the entity's media type), `scrudful/rest_framework.py` and `scrudful/fastapi.py` (re-export `Entity` in `__all__`)
- Test: `tests/test_entity.py`, `tests/test_ninja_entities.py`

**Interfaces:**
- Consumes: `media_type_key`, `is_in_band` and `context_defaults` (Task 1).
- Produces: `scrudful.entity.Entity`, a frozen dataclass.
  - **Fields:** the six existing fields, unchanged in meaning and order, then `media_type: str = "application/json"`.
  - **`__post_init__`** normalizes `media_type` with `media_type_key` (via `object.__setattr__`) and raises `ValueError`:
    - when the media type is in-band and `context_link_or_func` is set ("in-band content carries its own RDF; it takes no context link");
    - when the media type is neither JSON nor in-band, `context_link_or_func` is set, and `context_type_or_func` is not ("give context_type_or_func, the media type of the lifting description").
  - **Docstring:** says that link callables follow each adapter's calling convention: ninja `func(request, obj)`, DRF `func(view, request, **url_kwargs)`, FastAPI `func(**endpoint_kwargs)`.
  - `scrudful.ninja.Entity is scrudful.entity.Entity`, and likewise for `rest_framework` and `fastapi`.

- [ ] **Step 1: Write failing tests** in `tests/test_entity.py`:
  - `test_default_media_type`: `Entity().media_type == "application/json"`.
  - `test_media_type_normalized`: `Entity(media_type="Application/Problem+JSON").media_type == "application/problem+json"`.
  - `test_in_band_rejects_context`: `Entity(media_type="text/turtle", context_link_or_func="https://h/c")` raises `ValueError`.
  - `test_other_requires_context_type`: `Entity(media_type="text/csv", context_link_or_func="https://h/m")` raises; adding `context_type_or_func="application/csvm+json"` passes.
  - `test_reexported`: the identity checks above.

  In `tests/test_ninja_entities.py`:
  - `test_in_band_entity_sends_no_context_link`: a ninja operation with `Entity(schema_link_or_func=…, media_type="application/ld+json")` sends only the schema link.
  - `test_csv_entity_uses_rdf_context_rel`: `media_type="text/csv"` with a context link and `context_type_or_func="application/csvm+json"` sends `rel="https://w3id.org/semantic-http/ns#rdf-context"; type="application/csvm+json"`.
- [ ] **Step 2: Run** — `uv run pytest tests/test_entity.py tests/test_ninja_entities.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** In ninja, `_links(entity, …)` takes the context default from `context_defaults(entity.media_type)`, and sends no context link when that gives `(None, None)`. The schema defaults are `(SCHEMA_REL, SCHEMA_TYPE)`.
- [ ] **Step 4: Run the whole suite** — `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `core: Entity moves to scrudful.entity and declares its media type`.

### Task 3: django-ninja — the 0.1 HTTP Schema Resource and Link headers

**Files:**
- Modify: `scrudful/ninja.py`, mainly:
  - `ScrudfulRouter.__init__` (1176–1185) and its docstring (1116–1175): remove the `options_schema_link` and `options_context_link` parameters;
  - the module docstring "OPTIONS" section (≈ 270–297);
  - `_options_view` (2106–2147), `_options_link` (delete it), `_describe_path` (2153–2175), `_describe_operation` (2178–2227), `_media` (2259–2277);
  - `_add_links` (1916–1923) and `_link` (1951–1970).
- Modify: `examples/ninja/` (remove the `options_*` arguments).
- Test: `tests/test_ninja_options.py` (rewrite the 0.0-shape assertions), `tests/test_ninja_entities.py`, `tests/test_ninja_list.py`, `tests/test_ninja_create.py`, `tests/examples/ninja/test_example_project.py`, `examples/ninja/tests/test_example.py`.

**Interfaces:**
- Consumes:
  - `describe_content`, `describe_read`/`describe_write` (with `response_content`/`error_content`), `describe_options`, `allow_header`, `options_links`, `merge_links` and `resolve_uri` (Task 1);
  - `Entity.media_type` (Task 2).
- Produces:
  - `ScrudfulRouter(*args, **kwargs)`: exactly `ninja.Router`'s parameters. Passing `options_schema_link`/`options_context_link` raises `TypeError` (Python's own error; no shim).
  - The automatic OPTIONS answers per Global Constraints.

- [ ] **Step 1: Write failing tests** in `tests/test_ninja_options.py`:
  - `test_options_body_is_valid_0_1`: for each fixture path (item, list, create, search, error entity, `Entity()` with no links), `validate_http_schema_resource(body)` passes.
  - `test_options_headers`: status 200, `Content-Type: application/json`, the Link equals `options_links()`, and `Allow == allow_header(body)`.
  - `test_options_lists_options_and_head`:
    - `body["options"] == describe_options()`;
    - a path routing GET and HEAD has a `head` entry without `content`.
  - `test_relative_links_resolved`: an entity with `schema_link_or_func="/schemas/role"`:
    - its OPTIONS `$ref` is `"http://testserver/schemas/role"`;
    - the GET Link target is the same absolute URI.
  - `test_mixed_path_agrees` (Review Focus 5): a path with a scrudful GET and a plain ninja PUT:
    - `Allow == "GET, PUT, OPTIONS"`, in the body's order;
    - the `put` entry has no `requestBody` and no `content`.
  - `test_agreement_on_responses`: for the example project's item path, `link_problems(method, status, headers, has_content, hsr=body)` is `[]` for GET 200, HEAD 200, PUT 200, PATCH 200, POST 201, and scrudful's 412 and 428 with `error=`.
  - `test_view_link_not_duplicated`: a handler that sets `Link: <https://o/s>; rel="describedby"` keeps it, and the response has exactly one `describedby`.
  - `test_router_rejects_options_links`: `ScrudfulRouter(options_schema_link="x")` raises `TypeError`.
- [ ] **Step 2: Run** — `uv run pytest tests/test_ninja_options.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `_media(entity, request)` evaluates the entity's schema/context URLs and types, its callables included (with `(request, None)`, as today). It resolves the URLs with `resolve_uri(url, request.build_absolute_uri())` and returns `describe_content(entity.media_type, …)`, using `context_defaults` for a missing context type.
  - `_describe_path` builds the body in `METHOD_KEYS` order for the methods the path serves, plus `"options": describe_options()`. HEAD is described without content.
  - The OPTIONS view sets `Link = options_links()` and `Allow = allow_header(body)`.
  - `_link` resolves URLs the same way, so headers and the body agree.
  - `_add_links` uses `merge_links`.
- [ ] **Step 4: Update the other ninja tests and both example-project tests** to the 0.1 shapes. Run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `scrudful.ninja: Semantic HTTP 0.1 HTTP Schema Resource, Allow, absolute links`.

### Task 4: django-ninja — Links on error responses

**Files:**
- Modify: `scrudful/ninja.py`:
  - the wrapper path that calls `load` and the handler (`_decorator`, 1217 ff.);
  - the list slicing (`_slice`, 1671–1691, `raise HttpError(404, "Invalid page.")`);
  - a new public function `add_error_links`, added to `__all__`.
- Modify: `docs/source/ninja.rst` (an "Error responses" subsection).
- Test: `tests/test_ninja_errors.py` (new).

**Interfaces:**
- Consumes: `merge_links`, `resolve_uri` (Task 1); `Entity` (Task 2); ninja's `_links`/`_add_links` (Task 3).
- Produces:
  - **Inside a scrudful operation.** An `HttpError` or Django `Http404` raised by `load`, the handler or scrudful's own list paging is answered with the response ninja's API would give: call `operation.api.on_exception(request, exc)`, or the equivalent the implementer finds in ninja 1.7. That response gets the Links of `config.responses[status]` if declared, else of `config.error`. Link callables get `(request, None)`.
  - `add_error_links(api: NinjaAPI, error: Entity) -> None`. It wraps the exception handlers `api` would use for `ninja.errors.ValidationError`, `ninja.errors.AuthenticationError`, `ninja.errors.AuthorizationError` (if present in the installed ninja), `ninja.errors.Throttled`, `ninja.errors.HttpError` and `django.http.Http404`. Each wrapped handler keeps the original's response and adds `error`'s Links via `merge_links`, so a response that already has scrudful's Links is unchanged. Link callables get `(request, None)`.

- [ ] **Step 1: Write failing tests** in `tests/test_ninja_errors.py`:
  - `test_raised_http_error_uses_declared_entity`: an operation declaring `responses={404: ERR404}` that raises `HttpError(404, "x")` gets ERR404's Links and ninja's usual body/status.
  - `test_raised_http404_uses_error_entity`: with `error=ERR` and no `responses[404]`, a `load` raising `Http404` gets ERR's Links.
  - `test_list_invalid_page_has_error_links`: `?page=99` on a list with `error=ERR` → 404 with ERR's Links.
  - `test_add_error_links_validation_and_auth`:
    - after `add_error_links(api, ERR)`, a 422 (bad body) carries ERR's Links;
    - so does a 401 (no credentials);
    - a 200 is unchanged.
  - `test_add_error_links_idempotent_with_operation_links`: a scrudful 412 that already has `error=` Links is not doubled (exactly one `describedby`).
  - `test_agreement_for_declared_404`: `link_problems("GET", 404, headers, True, hsr=options_body) == []`.
- [ ] **Step 2: Run** — `uv run pytest tests/test_ninja_errors.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the two behaviours above. Write the docs subsection, covering what `add_error_links` covers and what it doesn't: ninja's own 405 for an unrouted method, and Django's 500 page.
- [ ] **Step 4: Run the whole suite** — `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `scrudful.ninja: Links on raised errors and an API-wide error entity`.

### Task 5: DRF — `error=` Links and the 0.1 HTTP Schema Resource

**Files:**
- Modify: `scrudful/rest_framework.py`:
  - `ScrudfulViewFunc.__init__`/`_handle`/link methods (131–328);
  - `scrudful` (330 ff.), `scrudful_api_view` (378 ff.), and `scrudful_viewset` with its `Meta` options (450–548);
  - `_install_metadata` (551–559), `ScrudfulMetadata` (562–663), `_conditional_response` (704–709), `_content` (716–728).
- Test: `tests/test_rest_framework.py`, `tests/examples/drf/test_api.py`.

**Interfaces:**
- Consumes: Task 1 core; `Entity` (Task 2).
- Produces:
  - **`error`.** `ScrudfulViewFunc(…, error: Entity | None = None)`, and the same keyword on `scrudful(...)` and `scrudful_api_view(...)`. `Meta` options `error` and `list_error` for `scrudful_viewset` (the latter configures the list). Callables use the DRF convention `func(view, request, **url_kwargs)`. scrudful's 400/412/428 bodies get `error`'s Links. A 304 gets none.
  - **Link headers.** Every link target is resolved with `resolve_uri(…, request.build_absolute_uri())`, and links are merged with `merge_links`.
  - **The OPTIONS body.** `ScrudfulMetadata.determine_metadata` describes exactly the view's `allowed_methods`, lower-cased and in `METHOD_KEYS` order, so DRF's own `Allow` equals the body's keys:
    - `get`/`post`/`put`/`patch`/`delete` as today, but with 0.1 content (`describe_content`) for the request and the success response, and `error`'s content on 400/412/428;
    - `head` as `describe_read(None, conditional)`;
    - `options` as `describe_options()`;
    - any other allowed method as `{"responses": {"200": {"description": "OK"}}}`.
  - **The OPTIONS response.** `_install_metadata(cls)` also installs an `options` method when the class uses DRF's stock `APIView.options`. The installed method calls it, then:
    - sets `request.accepted_renderer = JSONRenderer()` and `request.accepted_media_type = "application/json"` before returning, so the response is JSON even for `Accept: text/html`;
    - adds `options_links()` via `merge_links`;
    - adds the expose headers.
  - The public `ScrudfulMetadata.get_method` and `required_conditional_headers` keep their signatures.

- [ ] **Step 1: Write failing tests** in `tests/test_rest_framework.py`:
  - `test_error_links_on_412_428_400`: a viewset with `Meta.error = Entity(schema_link_or_func="/schemas/error", context_link_or_func="/contexts/error")`:
    - its 412, 428 and 400 bodies carry `<http://testserver/schemas/error>; rel="describedby"; type="application/schema+json"` and the JSON-LD context link;
    - a 304 has no Link.
  - `test_options_valid_0_1`: for the example app's item, list, read-only item and stats routes:
    - `validate_http_schema_resource(response.json())` passes;
    - the Link equals `options_links()`;
    - `Allow == allow_header(body)`;
    - the body has `head` and `options`.
  - `test_options_json_for_browsable_accept` (Review Focus 4): `Accept: text/html` gives `application/json` with the HSR Links.
  - `test_options_write_response_content`: the put/patch 200 and post 201 have `content` equal to the item's media type object.
  - `test_options_action_without_links` (Review Focus 5): an `@action` route without links has an entry with no `content` and no `requestBody`, and `Allow` still matches.
  - `test_agreement_drf_item`: `link_problems(...) == []` for GET, HEAD, PUT, PATCH, POST and the 412 (with `error`), against the OPTIONS body.
  - `test_view_link_not_duplicated`: as in Task 3, for DRF.
- [ ] **Step 2: Run** — `uv run pytest tests/test_rest_framework.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the above. `_conditional_response` gets the error Link value from `ScrudfulViewFunc`, computed with the callback arguments.
- [ ] **Step 4: Update the existing DRF OPTIONS assertions** to the 0.1 shape, then run `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `scrudful.rest_framework: error Links and the Semantic HTTP 0.1 HTTP Schema Resource`.

### Task 6: DRF — exception handler and a conformant example

**Files:**
- Modify: `scrudful/rest_framework.py` (a new public `exception_handler`, added to `__all__`)
- Modify: `examples/scrudful_rest_framework/`:
  - settings: `REST_FRAMEWORK["EXCEPTION_HANDLER"] = "scrudful.rest_framework.exception_handler"`;
  - give the list, `ReadOnlyItemViewSet` and stats routes schema and context links;
  - add an `error` entity, and the schema/context documents it serves.
- Modify: `docs/source/rest_framework.rst`.
- Test: `tests/test_rest_framework.py`, `tests/examples/drf/test_api.py`.

**Interfaces:**
- Consumes: `ScrudfulViewFunc.error` (Task 5).
- Produces: `exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None`.
  - It calls `rest_framework.views.exception_handler(exc, context)` and, if that returns a response, adds the Links of the `error` entity of the `ScrudfulViewFunc` handling `context["view"]`'s current method. For `ScrudfulViewFunc` lookup it uses `ScrudfulMetadata().get_method(view, view.action or method)`, or the view's handler for the request method.
  - Callables use the DRF convention.
  - Without a scrudful handler, or without `error`, the response is unchanged.

- [ ] **Step 1: Write failing tests:**
  - `test_exception_handler_adds_error_links`: with the example settings, the DRF serializer 400, the 404 from `get_object()` and the 405 for an unrouted method each carry the error entity's Links.
  - `test_exception_handler_without_error_unchanged`.
  - `test_example_links_everywhere`: in `tests/examples/drf/test_api.py`, every response from GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS on item, list, read-only and stats routes, plus the 304/400/412/428/404 cases, has `link_problems(...) == []` against that path's OPTIONS body.
- [ ] **Step 2: Run** — `uv run pytest tests/test_rest_framework.py tests/examples/drf -q`. Expected: FAIL.
- [ ] **Step 3: Implement**, update the example, and document the handler in `docs/source/rest_framework.rst`.
- [ ] **Step 4: Run the whole suite** — `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `scrudful.rest_framework: exception handler with error Links; conformant example`.

### Task 7: FastAPI — `error=` Links, absolute links and an error-links helper

**Files:**
- Modify: `scrudful/fastapi.py`:
  - `scrudful` (68–336): a new `error` keyword; resolve links against `str(request.url)`; use `merge_links`; attach the endpoint's configuration to the wrapper for Task 8;
  - `_conditional_response` (339–373);
  - a new `add_error_links`, added to `__all__`.
- Test: `tests/test_fastapi_adapter.py`.

**Interfaces:**
- Consumes: Task 1 core; `Entity` (Task 2).
- Produces:
  - **`error`.** `scrudful(…, error: Entity | None = None)`. Callables use the FastAPI convention: the endpoint's resolved keyword arguments. scrudful's 400/412/428 bodies get `error`'s Links. A Link a dependency set on the injected `Response` is still not copied onto these error responses: it describes the success representation.
  - **Configuration for Task 8.** The wrapper carries `wrapper._scrudful = _EndpointConfig(...)`, a frozen dataclass with:
    - the eight link and validator options;
    - `error`;
    - `request_name: str | None`, the endpoint's own `Request` parameter name.
  - **`add_error_links(app: FastAPI, error: Entity) -> None`.** It wraps the handlers `app` uses for `starlette.exceptions.HTTPException` and `fastapi.exceptions.RequestValidationError`; the defaults are `fastapi.exception_handlers.http_exception_handler` and `request_validation_exception_handler`, and handlers the app registered are kept. Each response gets `error`'s Links through `merge_links`. Link callables are called with `request=<Request>` only.

- [ ] **Step 1: Write failing tests:**
  - `test_error_links_on_early_responses`: a 412, 428 and 400 with `error=Entity(...)` carry its Links; a 304 none.
  - `test_relative_link_resolved`: `schema_link_or_func="/schemas/user"` gives `<http://testserver/schemas/user>`.
  - `test_view_link_not_duplicated`.
  - `test_add_error_links_http_and_validation`: after `add_error_links(app, ERR)`, an `HTTPException(404)` from the endpoint, a 422 and a 405 carry ERR's Links, and their bodies are FastAPI's defaults.
  - `test_add_error_links_keeps_custom_handler`: a custom `HTTPException` handler registered before the call still produces the body.
- [ ] **Step 2: Run** — `uv run pytest tests/test_fastapi_adapter.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite** — `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `scrudful.fastapi: error Links, absolute links and add_error_links`.

### Task 8: FastAPI — the OPTIONS helper

**Files:**
- Modify: `scrudful/fastapi.py` (a new public `add_options`, added to `__all__`)
- Modify: `examples/fastapi/api.py`:
  - route HEAD with `api_route(..., methods=["GET", "HEAD"])`;
  - call `add_error_links` and `add_options`;
  - give the endpoints an `error` entity and links.
- Modify: `docs/source/fastapi.rst`.
- Test: `tests/test_fastapi_adapter.py`, `tests/examples/fastapi/test_api.py`.

**Interfaces:**
- Consumes: `_EndpointConfig` (Task 7); `describe_*`, `allow_header`, `options_links` and `resolve_uri` (Task 1).
- Produces: `add_options(app: FastAPI | APIRouter) -> None`. Call it after the routes are added.
  - For every path with at least one `APIRoute` whose endpoint carries `_scrudful`, and with no route already serving OPTIONS, it registers `OPTIONS` (`include_in_schema=False`). That OPTIONS answers per Global Constraints.
  - The body describes the methods of all routes on that path, in `METHOD_KEYS` order, plus `options`:
    - a scrudful GET or HEAD gets `describe_read(...)`; HEAD has no content;
    - a write gets `describe_write(...)` with the request content, `response_content` equal to the endpoint's link content, and `error`'s content;
    - each route's success status is `route.status_code or 200`; a 204 has no content;
    - a non-scrudful route gets `{"responses": {"200": {"description": "OK"}}}`.
  - Link callables are called with `request.path_params`, plus the request under `request_name` when the endpoint declares one.
  - The registered OPTIONS has no dependencies and needs no credentials.

- [ ] **Step 1: Write failing tests:**
  - `test_add_options_body_and_headers`: `validate_http_schema_resource(body)` passes, the Link equals `options_links()`, and `Allow == allow_header(body)`.
  - `test_add_options_skips_existing_options`.
  - `test_add_options_mixed_path` (Review Focus 5).
  - `test_add_options_link_callable_gets_path_params_and_request`.
  - `test_example_links_everywhere`: as in Task 6, for the FastAPI example, covering GET/HEAD/PUT/PATCH/DELETE/POST/OPTIONS and 304/400/412/428/404/422/405.
- [ ] **Step 2: Run** — `uv run pytest tests/test_fastapi_adapter.py tests/examples/fastapi -q`. Expected: FAIL.
- [ ] **Step 3: Implement**, update the example, and document it in `docs/source/fastapi.rst`.
- [ ] **Step 4: Run the whole suite** — `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `scrudful.fastapi: add_options serves the Semantic HTTP 0.1 HTTP Schema Resource`.

### Task 9: ninja example conformance, docs, changelog and version

**Files:**
- Modify:
  - `examples/ninja/`: call `add_error_links(api, ERROR)` and route HEAD on the list;
  - `README.rst`, `docs/source/ninja.rst`, `docs/source/usage.rst` and `docs/source/index.rst`;
  - `CHANGELOG.rst`;
  - `pyproject.toml` and `uv.lock` (`uv version --bump minor` → `0.5.0`).
- Test: `tests/examples/ninja/test_example_project.py` (`test_example_links_everywhere`, as in Task 6).

- [ ] **Step 1: Write the failing test.** `test_example_links_everywhere` for the ninja example: every response with content, and every HEAD, on item, list and search paths is checked with `link_problems(..., hsr=<OPTIONS body>) == []`:
  - GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS;
  - 304/400/412/428/403/404/422/401.
- [ ] **Step 2: Run** — `uv run pytest tests/examples/ninja -q`. Expected: FAIL.
- [ ] **Step 3: Update the example.** Run it again and expect a PASS.
- [ ] **Step 4: Docs.**
  - Remove every "2019 draft" / "alignment … planned" passage and every `describedBy` / `options_schema_link` mention.
  - Add a "Semantic HTTP conformance" section to `README.rst`, mirrored in `docs/source/usage.rst`. It says what scrudful guarantees by default, and what the application must do:
    - install the error-links helper;
    - give every entity a schema and a context (only a schema for in-band types);
    - serve the linked documents;
    - make item contexts map every member;
    - handle 5xx pages;
    - put `OPTIONS` in the CORS `Access-Control-Allow-Methods` and `Link` in the middleware's expose list;
    - answer CORS preflights with CORS middleware: on a plain ninja `Router`, or behind `add_decorator` permission/throttle gates, scrudful's OPTIONS never sees them.
  - Run `uv run --group docs make -C docs html`; it must build without warnings.
- [ ] **Step 5: CHANGELOG** — a `0.5.0 (since 0.4.0)` section headed "Semantic HTTP 0.1 (breaking)". It lists:
  - the new rel/type defaults;
  - the 0.1 OPTIONS body, `Allow` and `options`/`head` entries, and the fixed OPTIONS Links;
  - the removal of `options_schema_link`/`options_context_link`;
  - absolute link resolution and link de-duplication;
  - `Entity` moved to `scrudful.entity` and gaining `media_type`;
  - `error=` for DRF and FastAPI;
  - `add_error_links` (ninja, FastAPI), `exception_handler` (DRF) and `add_options` (FastAPI).
  
  It references #4.
- [ ] **Step 6: Version** — `uv version --bump minor`, then confirm `pyproject.toml` says `0.5.0`.
- [ ] **Step 7: Full verification** — `uv run pytest -q && uv run black --check . && uv run flake8`. Expected: all pass.
- [ ] **Step 8: Commit** — `docs: Semantic HTTP 0.1 conformance; 0.5.0`.
