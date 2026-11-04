# scrudful 0.10.0: Semantic HTTP 0.1 at spec `37ec1ce` — design

Status: draft for the author's approval · 2026-11-04
Supersedes the remaining work of `2026-10-24-semantic-http-0.1-conformance.md`

## Intent

scrudful 0.5.0–0.9.0 was aligned to the Semantic HTTP spec at commit
`bcc338e` (2026-10-24). The spec's 0.1 merge `37ec1ce` (2026-10-25) replaced
the OPTIONS model the next day. An audit of 0.9.0 against `37ec1ce` found
every adapter non-conformant, with 8–12 normative statements per adapter
blocked: no option fixes them. The full audit is
`/private/tmp/claude-501/-Users-davidcharboneau-dev/26f8c9d0-4682-4a25-b1b1-23f1f0934b34/scratchpad/scrudful-0.9.0-vs-semantic-http-37ec1ce.md`.
Its headline findings are summarized in **Audit findings** below, so this
document stands alone.

The goal is the one the 0.1 conformance doc set: **conformant by default for
everything scrudful generates, with one documented option or helper for
everything the framework generates.** This release also fixes the
framework's own error pages (author).

scrudful's two consumers, django-scoped-rbac and scrud-django, are both
pinned to `scrudful>=0.8,<0.9`. A breaking release therefore can't reach them
until they move the pin themselves.

## Decisions the author made

1. **One breaking release, 0.10.0, covering all three adapters** (DRF,
   django-ninja, FastAPI). The consumer updates are follow-ups, outside this
   work. See **Follow-ups** for what each consumer will need.
2. **`Allow` is exact for item URLs.** OPTIONS on an item loads the object
   and lists only the methods this requester may use on it. A requester who
   may not PUT this object is not offered PUT.
3. **The HTTP Schema Resource is served by GET, on a route scrudful registers
   automatically**, with an option to mount it elsewhere instead (for
   scrud-django, beside its `collections-json-schema/<slug>/` documents).
4. **The framework's undescribed responses are fixed in this release:** JSON
   405s from scrudful's routers, plus 404/500 helpers that return JSON unless
   `Accept` asks for HTML.
5. **For DRF, one `include(...)` line in the root URLconf counts as
   automatic.**

Everything else below is the designer's call, marked *(my call)*, or is left
to the builder.

## Audit findings (scrudful 0.9.0 vs `37ec1ce`)

### Blocked in all adapters (shared code)

- **OPTIONS returns the HSR as a 200 `application/json` body.** The spec
  wants a body-less 2xx (SHOULD be 204) with exactly one HSR link:
  `rel="https://w3id.org/semantic-http/ns#http-schema-resource-document";
  type="application/json"`, with no `anchor`. `describe_options()`
  (`_adapter.py:611`) lists `options` as a 200 with content; the spec wants
  204 with no content.
- **No GET endpoint serves the HSR.** The spec's GET rules require
  `application/json`, a `describedby` link to
  `https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json` and a
  JSON-LD context link to `…/http-schema-resource.context.jsonld`.
  `options_links()` (`_adapter.py:562`) already builds those two links, but
  only for OPTIONS.
- **The HSR depends on the request.** URIs are resolved against the
  request's absolute URI, and DRF link callbacks get
  `(view, request, **url_kwargs)`. The spec says the HSR MUST NOT differ
  between requesters.
- **`Allow` lists the keys of the HSR** (`allow_header(body)`,
  `_adapter.py:629`), the same for every requester. The spec says it MUST
  list exactly the methods available to the requester.
- **No response carries the HSR link** (SHOULD: on every response,
  including errors, undescribed responses and HEAD).

### Configurable, or gaps, in shared code

- **`EXPOSABLE_HEADERS = ("ETag","Link","Location")`** (`utils.py:8`), and
  each is exposed only when present. The spec says MUST expose `Link, Allow`.
  A CORS middleware's own expose list replaces scrudful's.
- **A non-in-band entity or route with a schema link but no context link is
  accepted.** Its Media Type Object then lacks `x-rdf-context`, which fails
  the spec's JSON Schema.
- **No `default` error entry.** Framework 401/403/404/405/406/415/422/429
  responses carry content but aren't described.
- **`PROBLEM_JSON` relabels non-problem bodies.** ninja's and FastAPI's 422
  `{"detail": [...]}` are served as `application/problem+json` and fail the
  w3id problem-details schema (`detail` must be a string).

### Per adapter

- **ninja**
  - ninja's `text/html` 405, and Django's HTML 404/500, go to
    `Accept: application/json` clients.
  - Link callables get `obj=None` in the HSR but the loaded object on
    responses, so Agreement can break.
  - Preflights reach scrudful's OPTIONS and hit throttles (429 observed).
  - Plain operations on a `ScrudfulRouter` path are described without
    content.
- **DRF**
  - Success statuses are fixed to DRF's defaults: a POST `@action` returning
    200 is described as 201.
  - Success content is keyed `application/json` whatever the view returns
    (`_VIEW_MEDIA_TYPE`, `rest_framework.py:237`).
  - Error Agreement needs scrudful's `exception_handler`.
  - Without CORS middleware, preflights run permissions (403 observed).
  - The browsable API serves HTML to a request without `Accept` when it has
    `?format=api`.
- **FastAPI**
  - The endpoint media type is hard-coded to `application/json`
    (`fastapi.py:636, 1024`).
  - Only `route.status_code` is described; `responses=` is ignored.
  - Starlette's `text/plain` 500; `add_error_links` skips 500
    (`fastapi.py:538`).
  - Starlette's CORSMiddleware default `allow_methods=("GET",)` gives a 400
    on PUT/OPTIONS preflights.
  - A Link set by a dependency is dropped on scrudful's early 400/412/428
    (`fastapi.py:1416-1430`).

### Already right, and must stay right

- 0.1 Media Type Objects: every HSR body validates against the spec's
  schema at HEAD.
- `describedby` and `application/schema+json` defaults.
- Absolute URIs.
- Duplicate-link suppression, with the two context rels sharing one slot.
- In-band and non-JSON entities.
- Error Links on scrudful's own 400/412/428, and the error helpers.
- Agreement on scrudful's own responses.
- A routed HEAD gets GET's Links.

## Goals

1. **OPTIONS**, except a CORS preflight:
   - 204, no body;
   - `Allow` exact for the requester;
   - exactly one HSR link.
2. **The HSR is served by GET** at a URL that doesn't depend on the
   requester:
   - `application/json`;
   - the two w3id Links;
   - identical bytes for every requester;
   - cacheable (`ETag`, `Cache-Control: public`);
   - lists `options` as `{"responses": {"204": {"description": "No Content"}}}`.
3. **Every response scrudful produces or decorates carries the HSR link**:
   2xx, 304, early 400/412/428, errors through scrudful's error helpers,
   HEAD.
4. **Every HSR is complete and agrees with the wire:**
   - real success statuses;
   - real media types;
   - error responses, with a `default` entry from the API-wide error entity;
   - no Media Type Object that fails the spec schema.
5. **No undescribed response to a client that doesn't accept it**, from
   scrudful or from the framework under scrudful's helpers.
6. **CORS:**
   - `Link` and `Allow` are always exposed;
   - preflights are answered before auth, throttles and loading;
   - the docs give the middleware settings that keep conformance.
7. **Tests prove it.**
   - Re-vendor the spec fixtures from `37ec1ce`, adding the problem-details
     schema and context.
   - Write a wire-conformance suite per adapter that checks every rule the
     audit checked.
8. **Docs and CHANGELOG describe the new model.** Nothing describes the
   `bcc338e` OPTIONS any more.

## Non-goals

- Updating django-scoped-rbac or scrud-django (follow-ups).
- Automatic HEAD for GET-only operations. The documented
  `api_operation(["GET","HEAD"])` / stacked `@app.head` stays. *(my call)*
  The JSON 405 fixes the undescribed part.
- Accept-based content negotiation between several success media types.
  Describing a route's media types correctly is in scope; choosing between
  them per request is not.
- Moving to OpenAPI 3.2.
- Serving the w3id schema and context documents themselves.

## Anti-goals

- **An HSR that varies by requester, host or object.** If two requesters can
  get different bytes from the HSR URL, it fails, even if each document is
  valid.
- **Silent fallback.** If the HSR can't be built requester-independently (a
  relative link with no configured base URL), raise `ConfigError`. Never fall
  back quietly to the request's host.
- **OPTIONS doing the work of the method.** Computing `Allow` must not run
  handlers, consume throttle quota or write anything.
- **Breaking what already conforms** (the list above).

## Design

### 1. Shared core (`_adapter.py`, `utils.py`, `entity.py`)

- **New constants:**
  - `HSR_LINK_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"`;
  - `HSR_LINK_TYPE = "application/json"`;
  - `PROBLEM_DETAILS_SCHEMA_URL` / `PROBLEM_DETAILS_CONTEXT_URL` (the w3id 0.1
    URLs).
- **`describe_options()`** returns the 204 entry above.
- **`allow_header(body)` is replaced by `allow_header(methods)`:** it takes
  the methods the requester may use and formats them in `METHOD_KEYS` order.
  It always includes `OPTIONS`. `HEAD` follows `GET`. *(my call)*
- **`hsr_link(url)`** builds the HSR link-value.
- **`hsr_response_links()`** is the renamed `options_links()`: the Links of
  the GET response for the HSR.
- **`describe_content` raises `ConfigError`** for a non-in-band media type
  with a schema link and no context link. `Entity` raises at construction for
  the same case.
- **Base URL.** A new `base_url` setting (see 3). `resolve_uri` resolves
  relative links against it for both the HSR and the Link headers, so the two
  always agree. When it is unset:
  - absolute links pass through unchanged;
  - a relative link that must go into an HSR raises `ConfigError` the first
    time the HSR is built;
  - on Link headers, relative links keep today's request-based resolution,
    because RFC 8288 permits it. Agreement compares resolved targets, which
    match only when the request's host is the canonical one. The docs say
    to set `base_url`. *(my call)*
- **Expose headers.** `EXPOSE_HEADERS = ("ETag", "Link", "Location",
  "Allow")` is exported publicly, so it can be passed to CORS middleware.
  `Link` and `Allow` are always added to `Access-Control-Expose-Headers` on
  scrudful's responses; `ETag` and `Location` only when present. *(my call:
  this is harmless same-origin and required cross-origin.)*
- **Problem details.** A helper reshapes a framework validation body into
  problem details when the error entity's media type is
  `application/problem+json`. `detail` becomes a summary string, and the
  framework's error list becomes an `errors` extension member. The w3id
  context doesn't map `errors`, which is only a SHOULD gap. The docs say to
  publish a context that `@import`s the w3id one and maps `errors`. *(my
  call)*

### 2. Building the HSR: requester-independent

- **The HSR is built per path template**, not per URL: one document for
  `/roles/{pk}` and one for `/roles/`. It is built without a request.
- **Link callables in describe mode.** When building the HSR, link callables
  are called in a *describe* mode: `request=None`, `obj=None`, no URL kwargs.
  On live responses they are called as today. The docs state the rule: a
  link callable must return the same target in both modes, or Agreement
  breaks. *(my call)* Per adapter:
  - **ninja:** `func(None, None)`.
  - **FastAPI:** `func()`, with no endpoint kwargs.
  - **DRF:** `func(view, None)`, where `view` is a **request-less view
    instance**. It is built from the view function's `cls` and `initkwargs`,
    with `action` set and `args`/`kwargs` empty for a viewset: the same
    instance DRF's `as_view` would build, without dispatching. *(my call)*
    - Both consumers' callables read view attributes set through
      `initkwargs`: scrud-django's `resource_type_name`
      (`view_mixins.py:262-296`) and scoped-rbac's `resource_type_iri`
      (`rest.py:496-503`). `view=None` would leave them unable to tell which
      type they describe.
    - With a real instance, only their use of `request` has to change.
  - The alternative was to forbid callables in the HSR. That would break
    scrud-django's per-type links for no gain.
  - A callable that raises in describe mode (because it needs the request)
    surfaces as a `ConfigError` naming the callable, when the HSR is first
    built.
- **The HSR is cached per process** after its first build. Its `ETag` is a
  strong hash of the serialized bytes. *(my call; cache invalidation on code
  reload is not a concern.)*
- **Completeness inputs per adapter:**
  - **DRF.** Success statuses come from a new `statuses=` option on
    `@scrudful` / `Meta.statuses`, a mapping from method to status codes. The
    default is DRF's (201/204/200). Media types come from a new
    `Meta.entity` / `entity=` (an `Entity`, default `application/json`),
    replacing `_VIEW_MEDIA_TYPE`.
  - **FastAPI.** The route's `responses=` (status codes, and their `content`
    media types when given) and `status_code`. An `entity=` option on
    `@scrudful`, replacing `_ENDPOINT_MEDIA_TYPE`.
  - **ninja.** It already has entities and `responses=`. A plain, undecorated
    operation on a `ScrudfulRouter` path raises `ConfigError` when the router
    is built, unless it is given an `Entity`. *(my call: today it is silently
    described without content.)*
  - **All adapters: error entries.** Each method's `default` response entry
    has the content of the error entity that the method's own raised errors
    get: the operation's `error=` if it has one, else the API-wide entity
    (`add_error_links(...)` for ninja and FastAPI, `exception_handler` plus a
    new `SCRUDFUL["ERROR"]` setting for DRF). *(my call)*
    - Errors the framework raises before the operation runs carry the
      API-wide entity today: ninja's 401, 422 and 429, and DRF's 401, 403,
      406 and 415 from `initial()`.
    - When that entity differs from the operation's, those statuses get
      explicit entries with the API-wide content. Agreement picks an exact
      status key before `default`, so both agree with the wire.
    - No error changes which entity it carries.

### 3. Serving the HSR by GET

- **Automatic registration.** A `ScrudfulRouter` (ninja) and `add_options`
  (FastAPI) register a public GET route per path template.
- **DRF.** DRF has no hook scrudful can register through: scoped-rbac uses
  its own `DefaultRouter` subclass, scrud-django mounts views with
  `path(...)`, and `scrudful_api_view` views are on no router. So DRF needs
  **one line in the root URLconf** (author: "close enough" to automatic):

  ```python
  path("", include(scrudful.rest_framework.hsr_urls()))
  ```

  - `hsr_urls()` scans the URL resolver, lazily on the first request, for
    every pattern whose callback is a scrudful view: a `scrudful_viewset`
    class, or a view from `scrudful_api_view` or `@scrudful`. It finds them
    however they were mounted.
  - For each one it serves the HSR, built from the callback's `cls`,
    `initkwargs` and `actions` (see section 2, describe mode).
  - A scrudful view's responses find their HSR URL from
    `request.resolver_match`, which names the pattern.
  - Without the include, or with `Meta.hsr_route = False` /
    `SCRUDFUL["HSR_ROUTE"] = False` and no `hsr_url`, building raises
    `ConfigError` on the first OPTIONS or response of that view. Every
    scrudful response would carry an HSR link to nowhere, so failing loudly
    is better.
- **URL pattern.** One stable URL per path template, which cannot collide
  with an item key (a path parameter can't match it). Left to the builder;
  suggested: a reserved segment under the router's prefix, such as
  `<prefix>/_hsr/<name>.json`.
- **Behaviour of the route:**
  - no auth, permissions, throttles or CSRF;
  - `GET` and `HEAD`;
  - `application/json`;
  - `Link: hsr_response_links()`;
  - `ETag`;
  - `Cache-Control: public, max-age=…`, configurable;
  - answers `If-None-Match` with a 304, through `scrudful.conditional`;
  - sets no CORS headers of its own. The docs tell apps to have their CORS
    middleware cover the HSR route with the same origins as the resources
    (the SHOULD). *(my call: scrudful setting
    `Access-Control-Allow-Origin` itself would clash with django-cors-headers
    and Starlette's CORSMiddleware.)*
- **FastAPI key.** The FastAPI "path template" is the route's path string,
  as `add_options` groups routes today. Overlapping templates (a literal
  `/items/new` beside `/items/{id}`, or `/items/{id}` beside
  `/items/{item_id}` on another router) are separate resources with separate
  HSRs. The docs note that differently named parameters for one URL aren't
  merged. *(my call)*
- **Mounting elsewhere.** An option turns the automatic route off:
  `ScrudfulRouter(hsr_route=False)`, `Meta.hsr_route = False` /
  `SCRUDFUL["HSR_ROUTE"]`, `add_options(..., hsr_route=False)`.
  - Public functions return the document and a ready-made view for a path
    template, and an `hsr_url` setting names where it is mounted.
  - For a mounted HSR, `hsr_url` is required when the automatic route is
    off. Without it, building raises `ConfigError`.
  - This is how scrud-django puts it beside `collections-json-schema/<slug>/`.
- **Settings:**
  - Django adapters: a `SCRUDFUL` dict setting with `BASE_URL`,
    `HSR_ROUTE` and `HSR_CACHE_CONTROL`, overridable per router or viewset.
  - FastAPI: keyword arguments on `add_options`.
  - Names left to the builder.

### 4. OPTIONS

The order is fixed *(my call)*:

1. **Preflight short-circuit.** A request with `Origin` and
   `Access-Control-Request-Method` is answered 204, with no body and no Links,
   before auth, throttles, `before=`, permissions or loading. Any CORS
   middleware in front adds its headers; without one, the browser rejects the
   preflight, which is correct for a server that doesn't accept cross-origin
   requests.
2. **Authenticate, but don't gate OPTIONS itself.** OPTIONS runs the
   framework's authentication, so the requester is known, but no permission
   check for OPTIONS. An anonymous requester on a locked resource gets a 204
   with `Allow: OPTIONS`, not a 401 or 403. Bad credentials still answer as
   the framework does (401). *(my call)*
   - This matches scoped-rbac ("OPTIONS needs no grant",
     `permissions.py:197-199`) and keeps DRF and ninja alike.
   - DRF: `check_permissions` is skipped for OPTIONS in `initial()`.
   - ninja: the per-method `auth=` callbacks in step 4 do the authenticating.
   - Throttles: a non-preflight OPTIONS is throttled as the framework throttles
     it today. Computing `Allow` adds no throttle checks of its own.
3. **Load the object for an item path**, with the operation's own loader:
   - ninja: the GET operation's `load`, else the first item operation's;
   - DRF: `get_object()`, with `check_object_permissions` skipped for
     OPTIONS;
   - FastAPI: scrudful has no loader. `add_options(..., load=...)`
     (optional; `load(request, **path_params)`) gives one. Without it,
     `obj` is `None`, and the docs say exact object-level `Allow` needs
     `load=` or an `allowed=` hook that loads. *(my call)*

   Missing object (`None`, or `Http404`): continue with `obj=None`. A
   loader that raises any other HTTP error (for example scoped-rbac's 404 for
   an object the user can't read) answers with that error, through the
   adapter's error path, so it carries the error entity's Links and the HSR
   link.
4. **Compute `Allow`** (below).
5. **Answer** 204, with `Allow`, the HSR link and the expose headers.

#### Allow

For each method the resource serves, ask whether this requester may use it
on `obj`. `OPTIONS` is always listed. `HEAD` is listed when `GET` is.

- **Hook.** `allowed(request, method, obj) -> bool`, with each adapter's
  calling convention:
  - ninja: an argument on `ScrudfulRouter` and on each `@scrudful*`;
  - DRF: `Meta.allowed_func` / `allowed_func=`;
  - FastAPI: on `add_options`.

  When given, it alone decides.
- **Default without a hook:** the framework's own declared authorization,
  run with the method swapped. Each adapter has a definite list:
  - **DRF:** `check_permissions` and, when `obj` is not `None`,
    `check_object_permissions`. A request whose `method` is the method
    being checked is passed to the permission classes. A `PermissionDenied`
    or `NotAuthenticated` means not allowed.
  - **ninja:** the operation's `auth=` callbacks, then its `before=` hook,
    with a copy of the request whose `method` is swapped. A raised
    `HttpError` with 401, 403 or 404, an `AuthenticationError`, or a
    non-`None` return means not allowed. `after_throttles=`, throttles and
    `load` are not run per method.
  - **FastAPI:** the route's dependencies that declare `Security`, or are
    listed in `dependencies=`, resolved without body or parameter
    validation. A raised `HTTPException` with 401, 403 or 404 means not
    allowed. Dependencies that read the body are skipped and their method is
    allowed. The docs tell apps with object-level rules to give `allowed=`.
    *(my call: FastAPI has no permission classes, and running arbitrary
    dependencies risks side effects; the hook is the precise answer.)*
- **No object.** For a collection, or a missing object, the object-level
  checks are skipped.
- **Errors.** A check that raises anything not listed above propagates, as a
  500. It isn't swallowed.
- **405 responses** keep RFC 9110's `Allow`: the methods the resource
  supports, not filtered per requester.

### 5. The HSR link on every response

`merge_links` gains the HSR link. scrudful adds it to:

- every 2xx and 304 it decorates;
- the early 400/412/428;
- error responses through `add_error_links` / `exception_handler` on a path
  scrudful describes;
- HEAD.

A Link with the HSR relation that the view sets itself replaces scrudful's,
as for the other rels. A raised error on a path scrudful doesn't describe
(an unrouted 404) gets no HSR link.

FastAPI's early 400/412/428 keeps a Link set by a dependency on the injected
`Response`, merged by relation like the others.

### 6. Undescribed framework responses

- **405s.**
  - ninja: `ScrudfulRouter` answers an unrouted method on a known path itself,
    with a JSON 405 rendered through the API-wide error entity and its Links,
    plus `Allow`.
  - DRF already answers 405 in JSON.
  - FastAPI's 405 is JSON. With `add_error_links`, it carries Links.
- **Django 404/500 helpers** in `scrudful._django`, exported from both
  Django adapters: `handler404` / `handler500` factories taking the error
  entity.
  - They render problem details, or `{"detail": ...}`, unless `Accept`
    prefers `text/html` over the entity's media type. In that case they
    delegate to Django's default handlers.
  - They always send `Vary: Accept`.
  - Documented for `urls.py`.
- **FastAPI 500.** `add_error_links` also installs a handler for
  `Exception`, which sends a JSON 500 with Links. Starlette's
  `ServerErrorMiddleware` still re-raises in debug, which is documented.
- **DRF browsable API.** `?format=api` without `Accept: text/html` sends
  undescribed HTML. scrudful's docs recommend dropping
  `BrowsableAPIRenderer` in production, or keeping it reachable only through
  `Accept: text/html`, and scrudful sends `Vary: Accept`. *(my call: don't
  remove a DRF feature by default.)*

### 7. Tests

- **Fixtures.** Re-vendor `tests/fixtures/semantic-http-0.1/` from
  `semantic-http-spec` at `37ec1ce`:
  - `http-schema-resource.schema.json` and
    `http-schema-resource.context.jsonld`;
  - `person.http-schema-resource.json`;
  - plus `problem-details.schema.json` and `problem-details.context.jsonld`;
  - update the README's commit.
- **Wire-conformance suite per adapter**, ported from the audit harnesses
  in the scratchpad (`ninja/agree.py`, `drf/harness/run.py`,
  `fastapi/harness.py`). Each request/response pair is checked for:
  - an RFC 8288 Link parse;
  - one `describedby` link and one context link, with the right rel and
    type, on described content;
  - exactly one HSR link with the same target everywhere, and no `anchor`;
  - OPTIONS: 204 and empty;
  - the HSR GET rules;
  - HSR bytes identical across two requesters and two `Host`s;
  - schema validation of the HSR;
  - Agreement of every response with its HSR entry;
  - `Allow` per requester (anonymous, reader, writer, object-level denial);
  - preflight short-circuit, with throttles and auth;
  - no undescribed content for absent Accept and for
    `Accept: application/json`, including 404/405/500;
  - expose headers.
- **Existing tests.** Tests pinning the `bcc338e` shape are updated, not
  deleted. Every changed assertion maps to a CHANGELOG line.

### 8. Docs and CHANGELOG

- README, `ninja.rst`, `rest_framework.rst` and `fastapi.rst`:
  - the new OPTIONS and HSR model;
  - settings (`base_url`, the HSR route and mounting);
  - the `allowed` hook and its defaults per adapter;
  - the rule for describe-mode link callables;
  - the error helpers;
  - the CORS middleware settings (`expose_headers=scrudful.EXPOSE_HEADERS`,
    `allow_methods` including `OPTIONS`).
- Remove every statement of the old model:
  - "Allow lists the body's methods";
  - the OPTIONS body;
  - form parsers called non-conformant (now a MAY).
- **CHANGELOG 0.10.0, breaking.**
  - The OPTIONS wire format.
  - `Allow` semantics.
  - New and changed options:
    - `allow_header`'s internal signature;
    - `statuses=` and `entity=`;
    - `ConfigError` for a schema link without a context link;
    - `ConfigError` for an undecorated ninja operation;
    - `ConfigError` for an HSR with relative links and no base URL.
  - The HSR link on every response.
  - Expose headers.
  - Problem-details reshaping.
- Add a short migration section for consumers.

## Constraints

- Stdlib-only core: `_adapter`, `conditional`, `utils` and `entity` import
  no framework.
- Supported versions are unchanged: Python ≥ 3.10, Django ≥ 4.2,
  DRF ≥ 3.15, django-ninja ≥ 1.7 < 2, FastAPI ≥ 0.100.
  - ninja internals that scrudful already guards (`_run_checks`, `run`) stay
    guarded the same way.
  - Any new reliance on ninja internals, such as answering 405 in
    `ScrudfulRouter`, gets the same version guard and `ConfigError`.
- Existing behaviour not named here is unchanged:
  - preconditions, validators, paging;
  - `before=` and `after_throttles=`;
  - create preconditions (0.9.0).
- Work on a branch from the 0.9.0 commit (`8b07438`, on
  `fix/create-preconditions`). Merge 0.9.0 to master first if it isn't
  already.

## Left to the builder

- URL pattern of the automatic HSR route, within the constraints in 3.
- Setting and argument names.
- How each adapter swaps the method for permission checks: a copied DRF
  `Request`, a shallow-copied Django `HttpRequest`, or a modified Starlette
  scope.
- HSR cache layout and `max-age` default.
- How the wire-conformance harnesses are factored (one shared checker in
  `tests/semantic_http.py`, which already exists, is the obvious place).
- Release mechanics (existing `scripts/`).

## Follow-ups (not in this work)

- **django-scoped-rbac**
  - Move the pin to `>=0.10,<0.11`.
  - Implement `allowed(request, method, obj)` against its policy:
    `http.<METHOD>` permissions, policy cached on the request, conditions
    evaluated against `obj`.
  - Update `tests/semantic/test_contract.py:337-355`,
    `test_drf_front_end.py:336-363`, `test_ninja_crud.py:569`,
    `test_www_authenticate.py:205` and `tests/semantic/test_opt_out.py:173-183`.
  - Revisit `SemanticHTTPHeaders` stripping `Allow` (`rest.py:443-455`), so
    that OPTIONS 204 keeps it.
  - Replace its imports of private scrudful modules where public ones now
    exist.
- **scrud-django**
  - Move the pin.
  - Make the `reverse(..., request=request)` link callables work in describe
    mode (using `BASE_URL`).
  - Mount the HSR beside `collections-json-schema/<slug>/`.
  - Update `tests/test_rdf.py:33-58`, `tests/test_media_types.py:108-112` and
    `tests/test_upstream_drift.py`.
- **semantic-http-spec (possible).** The spec exempts a CORS preflight from
  requirement 3 and the OPTIONS rules, but not from requirements 1–2 or the
  undescribed-content rule. A middleware's `text/plain` "OK" preflight body
  (Starlette) is strictly non-conformant. Consider exempting preflight
  responses entirely.
