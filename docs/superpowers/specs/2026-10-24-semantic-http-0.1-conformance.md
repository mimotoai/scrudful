# scrudful and Semantic HTTP 0.1 — conformance review and alignment design

Status: draft for review · 2026-10-24 · tracks [issue #4](https://gitlab.com/dcharbon/scrudful/-/work_items/4)

## Why

The semantic-http spec (`~/dev/semantic-http-spec`, `source/*.rst`) merged its
0.1 revision (`spec-revision-0.1`, merged at `0b8af5b`). It makes breaking
changes to the wire format of version 0.0, the 2019 draft that scrudful 0.4.0
was built against (`docs/source/ninja.rst:49-52` says so). This document
records how far scrudful 0.4.0 is from 0.1, and what to change so that each
adapter is conformant by default where it can be and *enables* conformance
everywhere else.

Two questions were asked of every normative statement in the spec
(`tests/fixtures/normative-statements.txt`, 45 sentences):

- **Conformant by default?** With no configuration, are scrudful's responses
  conformant?
- **Enables conformance?** Can an implementer configure scrudful to be
  conformant, or does scrudful emit output they can't fix?

Each statement got one verdict per adapter: **Default** (conformant out of
the box), **Configurable** (conformant with a named option), **Implementer**
(scrudful neither helps nor hinders), **Blocked** (non-conformant output that
no option fixes) or **N/A**.

## Method

Three independent passes, all on scrudful 0.4.0 (`74ae484`):

1. Code audit of `scrudful.ninja`, plus the shared modules it uses.
2. Code audit of `scrudful.rest_framework`, `scrudful.fastapi`, and the shared
   core (`_adapter`, `_django`, `utils`, `conditional`, `collection`).
3. A black-box wire check. It drove the three example apps through in-process
   clients and recorded every response. It parsed Link headers per RFC 8288 and
   validated OPTIONS bodies with jsonschema (Draft 2020-12) against the spec's
   `source/_extra/0.1/http-schema-resource.schema.json`. It ran with and
   without CORS middleware (django-cors-headers 4.9, Starlette
   `CORSMiddleware`), and again with "tuned" apps that set every documented
   option to its 0.1 value. That separates defaults from structural limits.

The three passes agreed. The cited locations below were re-checked by hand.

## Verdict

**Not conformant by default, and only partly enables conformance.**

- **Links on success responses** are well formed and can be configured to be
  exact.
- **The OPTIONS document** (HTTP Schema Resource) is the 0.0 format in every
  adapter that has one, and no option changes that.
- **Error responses** mostly carry no Links. The spec requires them on every
  response with content, including 4xx/5xx and "a framework's default 404
  page" (`semantic_http_resource.rst`, "Responses covered").

| | Success-response Links | Error-response Links | OPTIONS / HTTP Schema Resource | Overall |
|---|---|---|---|---|
| ninja | Configurable (rel and type) | scrudful's 400/412/428 and a returned `Status`: Configurable via `error=` / `responses=`. Framework 401/404/422/405 and scrudful's list-page 404: none | **Blocked**: 0.0 format, no `Allow`, no `options` key, Links hard-coded `describedBy`/`application/json` | Closest, blocked on OPTIONS |
| DRF | Configurable (rel and type) | **None**, including scrudful's own 400/412/428 | **Blocked**: 0.0 format, no Links, `Allow` ≠ keys, writes list no response content | Blocked; a subclass+wrap workaround works |
| FastAPI | Configurable (rel and type) | **None**; scrudful also drops a Link a dependency set | **Not supported**: OPTIONS returns 405 | Requirement 3 left to the implementer |

## Findings

### F1. The OPTIONS document is the 0.0 shape — Blocked (ninja, DRF)

scrudful emits media type entries as `{"schema": "<url>", "context": "<url>"}`:

- ninja: `_media`, `scrudful/ninja.py:2259-2277`
- DRF: `_content`, `scrudful/rest_framework.py:716-728`

0.1 requires the Media Type Object form `{"schema": {"$ref": "<absolute URI>"},
"x-rdf-context": {"href": "<absolute URI>", "type": "<media type>"}}`
(`http_schema_resource.rst`, "Media Type Objects"). The alternatives are:

- `x-schema` (`{"href", "type"}`) for a schema that is not a JSON Schema;
- no `x-rdf-context` for an in-band media type.

Observed: every OPTIONS body that describes any content fails the 0.1 JSON
Schema. ninja `/api/roles/{pk}` gives 60 errors; DRF `/items/1/` gives 9. The
errors are always a string `schema`, an unknown `context` key, and a missing
`x-rdf-context`. A JSON-LD lifting with the 0.1 context turns `schema` into a
plain string and drops `context` entirely. Agreement can't hold, because it
compares against `$ref` and `x-rdf-context.href`.

Related:

- `requestBody` is emitted without `content` when the operation has no links:
  a non-scrudful PUT, or `request=Entity()` (`_adapter.py:299-306`). That is a
  schema error.
- **Relative URIs pass through.** `schema_link_or_func="/schemas/role"`,
  which the ninja docs' own examples use (`ninja.rst:84-88, 375-402`), puts
  `"/schemas/role"` in the OPTIONS body. 0.1 requires absolute URIs there.
  `link_content` (`utils.py:26-39`) never resolves them.
- The content map is hard-coded to `application/json`. In-band types (for
  example `application/ld+json`), non-JSON content and several media types per
  status can't be described.

### F2. The OPTIONS response itself — Blocked

- **Allow** must list exactly the method keys of the body.
  - ninja's OPTIONS has no `Allow` (`ninja.py:2119-2133`).
  - DRF sends `Allow: GET, PUT, PATCH, DELETE, HEAD, OPTIONS` for body keys
    `{get, put, patch, delete}`.
- **No `options` key** in any body (`_DESCRIBED_METHODS`, `ninja.py:2084`;
  DRF `_handlers`, `rest_framework.py:586`). There is no `head` key either,
  even where HEAD is served. The spec says "the HTTP Schema Resource lists"
  `options`.
- **Links of the OPTIONS response.**
  - DRF sends none.
  - ninja sends none unless `options_schema_link`/`options_context_link` are
    set. Their documented values are `example.com` placeholders.
  - Set to the w3id URIs, ninja's rel and type stay hard-coded to
    `describedBy`/`application/json` (`ninja.py:2124-2127`).
  - 0.1 requires targets
    `https://w3id.org/semantic-http/0.1/http-schema-resource.schema.json` and
    `…/http-schema-resource.context.jsonld`.
- **Completeness**
  - DRF lists no `content` for PUT/PATCH 200 or POST 201, and no list GET 200
    content, although those responses return JSON.
  - Non-scrudful operations are described without content.
  - Error entries are description-only. There is no `default` entry.
- **Static per requester.** The body is the same for every client, so `Vary`
  isn't needed (Default). But it lists methods the requester can't use
  (SHOULD NOT; `_handlers` ignores permissions).

### F3. FastAPI has no OPTIONS — requirement 3 unmet

`OPTIONS /users/1` returns `405`, `allow: GET`, `{"detail":"Method Not
Allowed"}`. Every HTTP Schema Resource rule is left to the implementer.

### F4. Error responses carry no Links — Blocked (DRF, FastAPI), partial (ninja)

Requirements 1 and 2 cover every response with content.

- **DRF and FastAPI: scrudful's own 400/412/428.** A failed precondition
  returns before Links are joined (`rest_framework.py:246-247`,
  `fastapi.py:246-247`), and neither adapter has ninja's `error=` entity.
  `_django.conditional_response` (`_django.py:38-48`) has no link input.
  FastAPI's early response also drops a Link a dependency set on the injected
  `Response`; only `Set-Cookie` survives.
- **ninja.**
  - `error=` covers scrudful's 400/412/428 and a returned `Status`.
  - A *raised* `HttpError(404)` gets no Links, even with `responses={404: …}`
    declared. The HSR then advertises content that the real response lacks,
    which violates Agreement.
  - scrudful's own list-page 404 (`raise HttpError(404, "Invalid page.")`,
    `ninja.py:1689`) ignores `error=`.
- **Framework errors carry no Links in any adapter.**
  - ninja: 401, 422, `Http404`, its `text/html` 405.
  - DRF: serializer 400, 401/403, 404, 405, and Django's HTML 404.
  - FastAPI: 404, 422, unrouted 404, 405.

  Implementers can fix these with exception handlers (ninja/FastAPI
  `exception_handlers`, DRF `EXCEPTION_HANDLER`/`finalize_response`), but
  scrudful ships none.

Issues #2 and #3 already track error Links for DRF and FastAPI (an `error`
entity at parity with ninja).

### F5. Defaults are 0.0 values — Configurable per route

`DEFAULT_SCHEMA_REL = "describedBy"` and
`DEFAULT_SCHEMA_TYPE = "application/json"` (`_adapter.py:19-20`).

0.1 says the relation type "MUST be `describedby`" and uses that single
spelling (`schema.rst`). The changelog lists "the registered `describedby`, in
lower case" as a breaking change. The `type` SHOULD be
`application/schema+json`. RFC 8288 compares relation types
case-insensitively, but the spec's own example linter matches `"describedby"`
exactly (`semantic-http-spec/tests/test_spec_text.py:92,137`), and so will any
client written the same way.

There is no global setting, so every decorator, `Meta` and `Entity` needs an
override, and the tests pin the old values (`test_rest_framework.py:51`,
`test_fastapi_adapter.py:33`). Neither example sets them.

### F6. Duplicate links — Default, unless the view sets Link

`join_links` (`utils.py:42-44`) appends without checking relation types. A
view that sets its own `describedby` Link ends up with two:

- ninja `_add_links`, `ninja.py:1916-1923`
- `rest_framework.py:257`
- `fastapi.py:261`

That breaks "MUST NOT include more than one" for both the Schema and the RDF
Context Link.

### F7. Links ignore the response's status and media type

- An error `Response` the view returns gets the success schema (FastAPI,
  observed).
- DRF's browsable-API HTML gets the JSON-LD context rel.
- Suppressing the context link for in-band content (SHOULD NOT), or using
  `https://w3id.org/semantic-http/ns#rdf-context` for non-JSON content, needs a
  callable. FastAPI callables only see the request if the endpoint declares it.

### F8. Smaller items

- **HEAD.** It returns 405 where not routed: FastAPI `/users/1`, ninja
  `/api/roles` (list). It is Configurable (`api_route`/`api_operation` with
  `"HEAD"`), and when routed it gets the same Links as GET.
- **DRF example.** The list, `ReadOnlyItemViewSet` and stats responses lack
  schema and/or context Links.
- **Examples.** None of them uses `describedby`, `application/schema+json` or
  CORS middleware.
- **CORS.**
  - scrudful adds `Access-Control-Expose-Headers: Link` (`_adapter.py:145-151`)
    but only when a Link is present.
  - Middleware with its own expose list replaces scrudful's. This is
    documented in the README.
  - Preflights are answered by the middleware (2xx, `OPTIONS` allowed: pass).
  - Without middleware, ninja's automatic OPTIONS answers a preflight with
    200 and no credentials (`auth=None`, `ninja.py:1207-1214`). A plain ninja
    `Router` returns 405, and `add_decorator` permission decorators and
    throttles also gate preflights.

### What already conforms

- The JSON-LD context relation `http://www.w3.org/ns/json-ld#context` with
  `type="application/ld+json"` is right by default, and the context link always
  has a `type`.
- No response has more than one link of either kind unless the view adds one
  (F6), and the removed 0.0 `rel="context"` never appears.
- HEAD gets the same Links as GET when routed.
- `Access-Control-Expose-Headers: Link` is added automatically.
- `scrudful.collection`'s list-envelope context maps every member at every
  level (checked with pyld), as the SHOULD asks.
- No Reference Objects. Every operation has `responses`, and its keys are
  status codes. There are no `x-` keys, and content keys are lowercase with no
  duplicates.

## Design

The aim: **conformant by default for everything scrudful generates; one
documented option or helper for everything the framework generates.**

### Shared core (`_adapter.py`, `utils.py`, `_django.py`)

1. **0.1 defaults.**
   - `DEFAULT_SCHEMA_REL = "describedby"` and
     `DEFAULT_SCHEMA_TYPE = "application/schema+json"`.
   - New constants for the HTTP Schema Resource's own schema and context URLs
     under `https://w3id.org/semantic-http/0.1/`, and for
     `https://w3id.org/semantic-http/ns#rdf-context`.
   - The in-band media types of `rdf_context.rst`.
2. **One Media Type Object builder** used by every adapter:
   - `{"schema": {"$ref": abs}}` when the schema type is
     `application/schema+json`, else `{"x-schema": {"href": abs, "type": t}}`;
   - plus `{"x-rdf-context": {"href": abs, "type": t}}` unless the media type
     is in-band.
3. **Absolute URIs.** Resolve relative link values against the request URI,
   both in the OPTIONS body and in Link headers, so the two always agree.
   (Alternative: raise `ConfigError`. Resolving keeps the docs' relative-link
   examples working.)
4. **Media types.**
   - An entity carries its media type (default `application/json`).
   - A status may have several media types: lowercase type/subtype, no
     duplicates.
   - The context link's rel follows the media type: the JSON-LD rel for JSON,
     `ns#rdf-context` otherwise, none for in-band.
5. **`describe_*`.**
   - Emit `head` when GET is served, and an `options` entry whose 200 points
     at the 0.1 schema and context.
   - List success content for writes.
   - Allow a `default` error entry.
   - Omit `requestBody` rather than emit one without `content`, or require a
     request entity.
6. **No duplicate links.** When the response already has a link with the
   `describedby` relation, or with an RDF context relation, keep the view's
   and don't add scrudful's. This matches "headers your view sets itself are
   never overwritten".
7. **Error links on early responses.** `_django.conditional_response` and its
   FastAPI counterpart take the error entity's links (this is #2/#3's `error`
   entity).

### OPTIONS response (all adapters)

- 200, `application/json`.
- Links to the w3id 0.1 schema and context, with `rel="describedby"; type=
  "application/schema+json"` and the JSON-LD context rel; on by default.
- `Allow` built from the body's method keys.
- Keep `options_schema_link`/`options_context_link` as overrides, or deprecate
  them. The placeholders go.

### ninja

- Switch `_media` to the shared builder.
- Apply `responses[status]` Links to *raised* `HttpError`s.
- Use `error=` for the list-page 404.
- An opt-in exception-handler helper adds the `error` entity's Links to
  `Http404`, `ValidationError`, `AuthenticationError` and `HttpError` responses.
- Document that preflights on a plain `Router`, or behind `add_decorator`
  gates, need CORS middleware.

### DRF

- Rewrite `ScrudfulMetadata` on the shared builder.
- Wrap `options` automatically with the HSR Links, and make `Allow` agree with
  the body.
- Error Links per #2.
- An `EXCEPTION_HANDLER` helper for DRF and Django errors.
- Fix the example's Link-less endpoints.

### FastAPI

- New: an OPTIONS helper that registers `OPTIONS` for a path from the
  configuration of its decorated endpoints: HSR body, Links, `Allow`.
- Error Links per #3. Keep a Link a dependency set on early responses.
- `exception_handlers` helpers for `HTTPException`/`RequestValidationError`.
- Route HEAD in the example.

### Tests

- Vendor the spec's 0.1 JSON Schema and JSON-LD context into `tests/fixtures`
  (CC BY 4.0, with attribution).
- Validate every OPTIONS body the tests produce against the schema.
- Add a wire-conformance test per adapter, porting the black-box checker:
  - one `describedby` link on every response with content, with `type`;
  - one context link with the right rel and type;
  - HEAD = GET Links;
  - Allow = keys;
  - Agreement between each response's Links and its HSR entry;
  - absolute URIs.

### Docs

- Drop the "2019 draft" note and update every rel/type in the README and docs.
- Add a "Semantic HTTP conformance" section that says what scrudful
  guarantees and what the application must do:
  - framework 5xx pages;
  - CORS `Access-Control-Allow-Methods`;
  - serving the schema/context documents at the link targets;
  - item contexts that map every member;
  - the CORS expose list.
- CHANGELOG: a breaking release (0.5.0). Every Link rel/type default and the
  OPTIONS body change on the wire.

## Spec notes (for semantic-http-spec, not scrudful)

- **Preflight content.** The spec exempts a CORS preflight only from
  requirement 3. Starlette's `CORSMiddleware` answers preflights with
  `text/plain` "OK" and no Links, which strictly violates requirements 1–2, and
  the resource can't control it short of replacing the middleware. Consider
  exempting preflight responses from requirements 1–2 as well, since browsers
  ignore their content (`browser_clients.rst` says so).
- **HEAD.** Agreement for HEAD uses "the entry for GET". Allow requires the
  listed methods to equal the body's keys, so any resource that serves HEAD
  must also list `head`. It may be worth saying so in Completeness.

## Tracking

Implementation: [issue #4](https://gitlab.com/dcharbon/scrudful/-/work_items/4). Related: #2 (DRF parity,
including error Links) and #3 (FastAPI parity, including error Links).
