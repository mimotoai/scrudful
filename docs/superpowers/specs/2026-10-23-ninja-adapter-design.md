# scrudful django-ninja adapter — design

Status: draft for review · 2026-10-23 · tracks [issue #1](https://gitlab.com/dcharbon/scrudful/-/work_items/1)

## Why

The owner is porting an API from Flask to django-ninja in order to use Django
apps: **django-scoped-rbac** (authorization) and **scrud-django** (semantic
CRUD resources). Both are DRF-based today and will be ported to ninja too. The
agreed order is:

1. **this adapter** (`scrudful.ninja`);
2. a ninja layer for django-scoped-rbac;
3. scrud-django rebuilt on 1 and 2;
4. the Flask app port.

The Flask API has no conditional requests today. From scrudful, the ported API
wants all of:

- lost-update protection (`If-Match` / 412 / 428);
- cheap revalidation for caching clients (304);
- the scrud conventions of the **semantic-http-spec**
  (<https://gitlab.com/dcharbon/semantic-http-spec>, cloned at
  `~/dev/semantic-http-spec`, `source/*.rst`).

The spec requires three things:

- a schema `Link` (`rel="describedBy"`) on every response with a body;
- a JSON-LD context `Link` on every response with a body;
- an OPTIONS response whose body is an "HTTP Schema Resource".

The spec is a 2019 draft that another session is updating. Where it is silent
or wrong (it has no PATCH, for example), follow this document.

The existing DRF adapter cannot be used with ninja. Issue #1 records the probe
against ninja 1.7.1:

- applied directly, the decorator gives a 500 on every request;
- attached through `decorate_view`, it runs *before* ninja's auth, so
  unauthenticated clients get 304s with real ETags, and 412s and 428s.

## The rule everything follows

**No precondition result and no validator may be computed from anything other
than the object (or queryset) that authorization already approved.**

scoped-rbac's own DRF viewsets break this today (scoped-rbac issue #14): their
ETag callbacks do an unscoped `get_object_or_404`, which leaks existence and
modification time. In ninja the per-object authorization decision happens when
the operation fetches the object; ninja has no `has_object_permission` hook.
So the adapter takes a **load** function from the implementer and computes
everything from what it returns.

## What scrudful is

scrudful is a **utility for building semantic-http-spec-compliant endpoints**.
It implements HTTP and the spec faithfully and gives implementers the places to
plug in their policy. It does **not** dictate application policy. For example:

- whether PUT may create;
- how missing and hidden resources are reported;
- whether lists have validators.

Where a choice has a security trade-off, the adapter documents the trade-off
and the patterns, and leaves the choice to the implementer.

## Goals

A `scrudful.ninja` module that makes ninja operations SCRUDful. It covers:

- single resources;
- collection GET in the scrud list envelope;
- create POST and general POST;
- OPTIONS;
- error bodies.

It must give the same HTTP behaviour as the DRF and FastAPI adapters wherever
they overlap, because they share `scrudful/conditional.py` and
`scrudful/_adapter.py`.

## Non-goals

- **Permission-aware OPTIONS.** The owner decided: OPTIONS describes the same
  API to everyone.
- **Serving collection schema/context documents.** The adapter *builds* them
  (see Collections); the application or a scrud-django port serves them.
- **Changing the DRF or FastAPI adapters' public behaviour.** The new
  behaviours introduced here are:
  - 201 validators;
  - the request/response entity declarations;
  - error Links;
  - load-based validators.

  Port them to DRF and FastAPI later, and open one follow-up issue per adapter.
  The only DRF change in scope is moving its Django helpers into a shared
  module, with no behaviour change.
- **Implementing scoped-rbac's ninja layer or scrud-django's port.** The
  adapter only has to accept what they will provide: a fetch-if-allowed
  callable, and a handler that returns the authorized queryset.
- **Updating the semantic-http-spec.**

## Anti-goals (failure even if it "works")

- Any 304, 400, 412 or 428, or any `ETag` / `Last-Modified`, produced before
  ninja's auth, throttling or `load` has run, or computed from an unscoped
  lookup.
- scrudful itself telling a hidden resource apart from a missing one. Every
  scrudful-produced status, header and body must be a function of what `load`
  returned (or raised) and the request, so the implementer's choice in `load`
  fully decides what a client can observe.
- Exceptions from `load` or the callbacks bypassing ninja's exception handlers.
  Issue #1 saw Django's HTML 404 page through `decorate_view`.
- Requiring DRF to import `scrudful.ninja`.
- Breaking any existing DRF or FastAPI test.

## Constraints

- Python 3.10–3.14. The CI matrix in `.gitlab-ci.yml` runs every test on all
  five.
- The package itself has no runtime dependencies. Add an optional extra
  `ninja = ["django>=4.2", "django-ninja>=…"]`. The builder picks the floor:
  the oldest 1.x whose APIs are used, verified in tests. 1.7.1 is the version
  probed.
- `scrudful/conditional.py` and `scrudful/_adapter.py` stay stdlib-only.
- Tests run with `filterwarnings = ["error"]`, and black and flake8 must pass.
- **Releasing.** This change touches `scrudful/`, so the MR must bump the
  version (`version-check` job; README "Releasing"). The planned version is
  **0.4.0**: `uv version --bump minor`. Merging the MR publishes it.
- Docs:
  - a `docs/source/ninja.rst` page;
  - a README section;
  - a `CHANGELOG.rst` entry;
  - an update to the `pyproject.toml` description and classifiers (add the
    django-ninja framework classifier if one exists).

## Decided design

### 1. Single-resource operations (GET, HEAD, PUT, PATCH, DELETE, and POST actions on an item)

```python
@router.put("/roles/{pk}", auth=django_auth, response=RoleOut)
@scrudful(
    load=authorized_role,                    # fetch-if-allowed; returns the object, None, or raises
    etag_func=lambda role: role.etag,
    last_modified_func=lambda role: role.modified_at,
    schema_link_or_func="/schemas/role",
    context_link_or_func="/contexts/role",
)
def update_role(request, pk: int, payload: RoleIn, role: Role): ...
```

(The names are illustrative except where this document fixes them.)

Order of execution:

1. ninja's auth and throttling run, as for any operation. 401 and 429 are
   unchanged.
2. ninja resolves and validates the parameters.
3. `load` runs with the request and ninja's validated parameters. It returns:
   - the object;
   - `None`, meaning "no current representation", with the same semantics as
     validator callbacks returning `None` today. This is RFC 9110 behaviour,
     implemented by `evaluate_preconditions`: on PUT, which may create,
     `If-None-Match: *` passes and `If-Match` gives 412; other methods proceed
     to the handler, which produces its own response (e.g. 404);
   - or it raises (e.g. `Http404`, `HttpError(404)`), which goes through
     ninja's exception handlers.

   The adapter allows both `None` and raising, for every method. It does not
   decide whether PUT may create. That is the implementer's policy, expressed
   through what `load` returns.
4. The validator callbacks are called **with the loaded object**, and
   `evaluate_preconditions` decides. The rules match the DRF and FastAPI
   adapters:
   - 304 for a matching GET/HEAD;
   - 412 for a failed precondition;
   - 428 for an unconditional PUT/PATCH/DELETE on an existing resource;
   - 400 for a malformed `If-Match` / `If-None-Match`.

   POST is evaluated but never required to be conditional.
5. The handler runs and receives the loaded object, so nothing is fetched
   twice.
6. The response gets:
   - validators on 2xx (fresh ones for PUT/PATCH, describing the new
     representation; see `_adapter.validator_policy`);
   - the response entity's schema and context `Link` headers;
   - `Access-Control-Expose-Headers`.

**Choosing what `load` returns.** This must be documented in
`docs/source/ninja.rst`, with an example of each pattern. The owner decided
that scrudful should not choose for the implementer.

| Pattern | `load` for missing | `load` for hidden | Consequence |
|---|---|---|---|
| **No PUT-create** | raise 404 | raise 404 | Missing and hidden are indistinguishable for every method. Creation goes through POST to the collection. |
| **PUT-create, nothing hidden** | `None` | (n/a) | RFC semantics: `If-None-Match: *` creates, `If-Match` gives 412. |
| **PUT-create with hidden rows** | `None` | raise 404 | A PUT with `If-Match` tells them apart (412 vs 404). Acceptable only when identifiers aren't guessable or existence isn't secret. |
| **PUT-create, hidden treated as missing** | `None` | `None` | The handler must cope with a create that collides with a hidden row. It must not overwrite the row or 500 on it; a 409 or 404 is a policy choice. |

**`load` is required whenever `etag_func` or `last_modified_func` is set** (a
`TypeError` at decoration time otherwise). This is my call: it makes the safe
path the only path. Operations without validators still get Links and
OPTIONS, and need no `load`.

### 2. Create POST on a collection

- Authorization is decided by the handler from the parsed body; scoped-rbac
  checks the body's `rbac_context`. There is no `load` and no 428.
- A successful response is **201** with:
  - `Location` (the new item's URL);
  - the new item's schema and context `Link` headers;
  - **the new item's `ETag` and `Last-Modified`**, from the item validator
    callbacks applied to the created object. This is my call, made so a client
    can edit right away without a GET.

### 3. General POST (e.g. `POST /roles/search`)

- The default is **200 with the result entity** in the body, no `Location`, and
  no validators unless the implementer sets them.
- The request entity (the query) has its own schema and context, distinct from
  the response's.

### 4. The declaration model and the runtime "place on the response"

Every scrudful operation can declare:

- a **request entity**: a schema link and a context link, each with rel and
  type, following the existing `*_link_or_func` / `*_rel_or_func` /
  `*_type_or_func` conventions;
- a **response entity per status** (schema and context).

**Link values** are a string or a callable `func(request, obj)`. This is my call:
one signature that works everywhere. `obj` is whatever the operation has at
that point:

- the loaded item (§1);
- the created item (§2);
- each item while building the collection envelope (§5);
- the authorized queryset for collection-level Links;
- `None` for OPTIONS and general POST.

Rel and type follow the same rule. OpenAPI constants never call link
callables.

"Create" is a preset of this model (201, `Location`, the item entity and item
validators), not a separate code path. The declarations are static and feed:

- OPTIONS;
- the default response headers;
- the OpenAPI constants.

The handler has **one documented runtime place** to set, for this response:

- the status;
- `Location`;
- `ETag` / `Last-Modified`;
- a schema or context Link that overrides the declared one;
- the created object to compute validators from.

The owner asked specifically that implementers be given "the correct places to
provide the needed metadata and values on the response". Explicit values set
there win over computed ones, and headers set directly on the response are
never overwritten, as today.

### 5. Collections (GET on a list)

The list operation works like ninja's own `@paginate`. This is my call, chosen
so it looks familiar to ninja users:

- **The handler returns the authorized queryset.** scoped-rbac's list filter
  is applied inside it, and there is no `load`.
- The implementer declares `response=List[RoleOut]`.
- The adapter replaces that response schema with a **typed envelope schema**
  that wraps `RoleOut`, so ninja's validation and OpenAPI describe the real
  body. Use the same mechanism `@paginate` uses to contribute to the
  operation.

The adapter paginates the queryset and builds the scrud envelope:

```json
{
  "count": 240, "page_count": 3,
  "first": "…?page=1", "previous": "…?page=1", "next": "…?page=3", "last": "…?page=3",
  "content": [
    {
      "href": "https://…/roles/17",
      "etag": "\"…\"",
      "last_modified": "Thu, 22 Oct 2026 21:09:56 GMT",
      "link": "</schemas/role>; rel=\"describedBy\"; type=\"application/json\", </contexts/role>; rel=\"http://www.w3.org/ns/json-ld#context\"; type=\"application/ld+json\"",
      "content": { "…the item rendered by the operation's item Schema…": "" }
    }
  ]
}
```

- `count` counts only the authorized items.
- Each item's `etag`, `last_modified` and `link` come from **the same item
  callbacks the detail operation uses**. `href` comes from an item-URL
  callback.
- `first` and `last` are included because scrud-django's published collection
  schema describes them, although its paginator never emitted them. This is my
  call.
- Pagination uses the `page` and `page_size` query parameters, with a default
  of 100 and a maximum of 1000, copying scrud-django's
  `StandardResultsSetPagination`. This is my call.
- `previous`, `next`, `first` and `last` are absolute URLs, with `previous` and
  `next` set to `null` at the ends.
- Response headers are the **collection** entity's schema and context Links.
- **Optional collection validators** (as DRF's `list_etag_func` /
  `list_last_modified_func`) are called with the authorized queryset. When they
  are set, a list can answer 304.
- The same path serves the create POST (§2) and OPTIONS (§6).

**Collection documents.** The adapter provides a public function that, given
the item schema URI and context URI (and a type IRI), **builds** the
collection JSON Schema and JSON-LD context documents describing the envelope
above. The starting point is scrud-django's `ResourceCollectionSchemaView` and
`ResourceCollectionContextView` in `~/dev/scrud-django/scrud_django/views.py`.
Note that their content-array schema is malformed: it nests `type`/`items`
under `properties`. Fix that, don't copy it. Serving the documents is the
application's job. The list's Links point to URLs the implementer configures,
as in DRF.

### 6. OPTIONS

- The adapter adds an OPTIONS operation **automatically** for every path that
  has at least one scrudful operation. The implementer declares nothing. An
  OPTIONS operation the implementer declares on that path wins.
- The body is the spec's **HTTP Schema Resource**: for each method the path
  serves (get, put, patch, post, delete, plus head if it is routed):
  - the `requestBody` and `responses`, with `content` mapping each media type
    to `{"schema": <uri>, "context": <uri>}`;
  - for conditional writes, the precondition header parameters.

  **Match the shape DRF's `ScrudfulMetadata` produces**
  (`scrudful/rest_framework.py`, `class ScrudfulMetadata`) wherever both can
  express the same thing. The ninja body is a **superset**: it adds response
  `content` on PUT/PATCH/POST (as the spec's example does), the per-status
  entities of §4, and extra statuses declared on the operation. Parity is
  judged only on what DRF expresses. This is my call. Take descriptions and
  status codes from ninja's operation definitions where available.
- **The same for everyone.** OPTIONS does not run `load`, a list handler
  or any permission check, and it ignores the API's or router's `auth`, like
  ninja's public docs. It describes the path template, not an item, so it
  reveals nothing about existence. This is my call, derived from the owner's
  decision.
- The OPTIONS response carries Links to the semantic-http schema and context of
  the HTTP Schema Resource itself. The URLs are configurable. When unset, they
  are omitted (not spec-conformant, and documented as such). This is my call:
  the spec's example `scrud.io` URLs aren't known to resolve.
- PATCH is included even though the 2019 spec omits it.
- CORS preflight: browsers send OPTIONS preflights without credentials.
  `django-cors-headers` answers preflights in middleware before the view.
  Document that scrudful's OPTIONS is the non-preflight case, and test that a
  request carrying `Access-Control-Request-Method` isn't broken by the adapter.

### 7. Errors

When the implementer configures an error schema and context, scrudful's own
400, 412 and 428 JSON bodies carry them as Links. Without that configuration
they are sent as today. The spec requires Links on every response with a body,
and its example declares error schema/context. This is my call.

### 8. Async, OpenAPI, structure

- **Sync and async operations both work.** An `async def` handler stays async
  so ninja builds an `AsyncOperation`. Sync `load` and sync callbacks inside an
  async operation run via `sync_to_async`, because the Django ORM requires it.
- **Wrap the endpoint function**, not `Operation.run`, so everything runs
  inside the operation after `_run_checks`.
- Set headers through ninja's temporary response (a parameter annotated
  `HttpResponse`), and add that parameter when the endpoint lacks it. This is
  issue #1's approach, mirroring the FastAPI adapter's `Response` handling.
  Early responses (304/400/412/428) are returned as `HttpResponseBase`, which
  ninja passes through. Keep cookies, and on 304 keep cache headers, following
  the FastAPI rules.
- Work out the response status for the 2xx-only validator rule from:
  - a returned status tuple or `Status`;
  - otherwise the operation's single declared response code;
  - otherwise 200.
- The loaded object reaches the handler through a handler parameter that is
  **hidden from ninja's signature and OpenAPI** (it must not become a query
  parameter).
- Ship OpenAPI constants for the 304/400/412/428 responses, like
  `CONDITIONAL_READ_RESPONSES` and `CONDITIONAL_WRITE_RESPONSES` in
  `scrudful/fastapi.py`.
- Move the Django-specific helpers shared with the DRF adapter into a private
  `scrudful/_django.py`:
  - naive datetimes read in `settings.TIME_ZONE`;
  - building `ConditionalResult` responses;
  - reading precondition headers from a Django request.

  `scrudful/rest_framework.py` uses that module with no behaviour change.

## Left to the builder

- Public names and module layout inside `scrudful/ninja.py`:
  - the decorator(s): one decorator with modes, or separate decorators for
    item, collection and create;
  - the runtime response object;
  - how OPTIONS gets registered (a router subclass, a function applied to a
    router or API, or registration at decoration time).

  Constraint: the implementer never declares OPTIONS.
- How fresh validators for PUT/PATCH, and 201 validators, obtain the new
  object. Options include calling `load` again, the runtime place, or the
  handler's return value. The constraint is that they describe the
  representation just written.
- Whether HEAD is routed automatically. Match what ninja does for GET, and
  test it.
- The django-ninja version floor, and Django range of the `ninja` extra.
- The IRIs used inside the generated collection documents. scrud-django
  hard-coded `api.openteams.com`. Make them parameters with neutral defaults,
  and don't hard-code openteams.
- Test structure. Follow the existing suites (`tests/test_fastapi_adapter.py`,
  `tests/test_rest_framework.py`) and add `examples/ninja/`.

## Acceptance criteria

- Unauthenticated requests get ninja's 401 (or throttling's 429) before `load`,
  any callback or any precondition evaluation runs. Add a regression test for
  each row of issue #1's table.
- When `load` raises, the client gets `load`'s response (e.g. 404) for every
  method and every `If-*` combination, never a 304/412/428 or a validator.
- When `load` returns `None`, the result follows RFC 9110 exactly as
  `evaluate_preconditions` defines it (PUT-create with `If-None-Match: *`, 412
  for `If-Match`, other methods reaching the handler). There is a test for
  each pattern in the "Choosing what `load` returns" table, and the docs page
  shows each one.
- GET/HEAD send `ETag` / `Last-Modified` on 2xx, and 304 for a matching
  `If-None-Match` / `If-Modified-Since`.
- PUT/PATCH/DELETE:
  - 428 without a precondition;
  - 412 when it is stale;
  - fresh validators on 2xx.
- Create POST returns 201 with `Location`, the item Links and the item
  validators. General POST returns 200 with the result Links and no validators
  by default.
- A collection GET returns the envelope above, scoped to the queryset the
  handler returned, with per-item validators equal to those of the detail GET.
  Pagination limits are enforced, and the OpenAPI response schema is the typed
  envelope.
- The collection-document builder produces a valid JSON Schema (validated in a
  test) and a JSON-LD context for the envelope.
- OPTIONS:
  - is answered on every scrudful path without being declared, and without
    auth;
  - has the HTTP Schema Resource shape;
  - carries its own Links when configured;
  - is a superset of DRF's output for an equivalent view, identical on the
    parts DRF expresses.
- When configured, the error Links are on scrudful's 400/412/428 bodies.
- Sync and async operations behave identically.
- Exceptions from `load` and the callbacks go through ninja's exception
  handlers.
- 304/412/428 appear in OpenAPI when the constants are used.
- `scrudful.ninja` imports without DRF installed. Test it in a subprocess or an
  environment without DRF.
- All existing tests pass unchanged.
- CI is green on 3.10–3.14, including `version-check` with 0.4.0.
- An example ninja project in `examples/ninja/` has a Role-like resource using
  every feature above. A scoped-rbac ninja layer could be written against it
  without working around anything.
- Follow-up issues are open for porting to the DRF and FastAPI adapters:
  - 201 validators;
  - entity declarations;
  - error Links;
  - load-based validators.

  Add a comment on scoped-rbac #14 noting that `RoleAssignmentViewSet` has the
  same unscoped lookup. Post it only with the owner's go-ahead at that point.

## References

- Issue #1 (probe findings and the earlier adapter outline):
  <https://gitlab.com/dcharbon/scrudful/-/work_items/1>
- semantic-http-spec: `~/dev/semantic-http-spec/source/`. See
  `restful_json_api.rst`, `semantic_http_resource.rst` and
  `http_schema_resource.rst`.
- django-scoped-rbac issue #14 (the ETag-before-object-permission leak):
  <https://gitlab.com/dcharbon/django-scoped-rbac/-/issues/14>
- scoped-rbac's request-time checks: `~/dev/django-scoped-rbac/src/scoped_rbac/`
  - `permissions.py`: `policy_for`, `IsAuthorized`;
  - `filters.py`: `RbacFilter`;
  - `rest.py`.
- scrud-django:
  - the envelope: `scrud_django/mixins/serializer_mixins.py`;
  - pagination: `paginations.py`;
  - collection documents: `views.py`.
- The existing adapters:
  - `scrudful/fastapi.py`, which is the structural model for ninja;
  - `scrudful/rest_framework.py`, for `ScrudfulMetadata` and the list options;
  - `scrudful/_adapter.py`.
