"""Digest the django-ninja internals scrudful.ninja checks, for a ninja release.

Usage: ``python -I scripts/ninja_digests.py <dir> [--code]``, ``<dir>`` holding
the release's ``ninja`` package (e.g. from ``uv pip install --target <dir>
--no-deps django-ninja==<version>``); compare its output with
``_NINJA_SOURCES`` (and, with ``--code``, ``_NINJA_CODE``) in
``scrudful/ninja.py`` before allowing the release in the ``ninja`` extra.

It reads the source text and never imports the release: each method is found
with ``ast`` and digested as ``scrudful.ninja._source_digest`` digests it (its
tokens, comments, blank lines and docstring aside); ``--code`` adds the names,
locals and argument count of its compiled code, the check scrudful makes where
ninja's source isn't installed (they depend on the Python running the script:
run it with each Python whose fingerprint differs). Methods whose digest
differs from the listed releases' need reading: add the digest only when the
change doesn't affect what scrudful relies on.
"""

from __future__ import annotations

import ast
import hashlib
import io
import pathlib
import sys
import textwrap
import tokenize
from typing import Any

#: As ``scrudful.ninja._IGNORED_TOKENS`` and ``_STRUCTURE_TOKENS``.
IGNORED = frozenset({tokenize.COMMENT, tokenize.NL})
STRUCTURE = frozenset(
    {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.ENDMARKER}
)

#: Each name of ``_NINJA_SOURCES``: its module (below ``ninja/``), class and
#: method.
TARGETS = {
    "Operation.clone": ("operation.py", "Operation", "clone"),
    "Operation._check_throttles": ("operation.py", "Operation", "_check_throttles"),
    "Operation._run_authentication": (
        "operation.py",
        "Operation",
        "_run_authentication",
    ),
    "Operation._run_checks": ("operation.py", "Operation", "_run_checks"),
    "Operation.run": ("operation.py", "Operation", "run"),
    "AsyncOperation._run_authentication": (
        "operation.py",
        "AsyncOperation",
        "_run_authentication",
    ),
    "AsyncOperation._run_checks": ("operation.py", "AsyncOperation", "_run_checks"),
    "AsyncOperation.run": ("operation.py", "AsyncOperation", "run"),
    "PathView.clone": ("operation.py", "PathView", "clone"),
    "PathView.get_view": ("operation.py", "PathView", "get_view"),
    "PathView._sync_view": ("operation.py", "PathView", "_sync_view"),
    "PathView._async_view": ("operation.py", "PathView", "_async_view"),
    "NinjaAPI._get_bound_routers": ("main.py", "NinjaAPI", "_get_bound_routers"),
    "NinjaAPI._get_urls": ("main.py", "NinjaAPI", "_get_urls"),
    "BoundRouter._bind_operations": ("router.py", "BoundRouter", "_bind_operations"),
    "BoundRouter.urls_paths": ("router.py", "BoundRouter", "urls_paths"),
    "ParamModel.resolve": ("params/models.py", "ParamModel", "resolve"),
    "PathModel.get_request_data": (
        "params/models.py",
        "PathModel",
        "get_request_data",
    ),
    "NinjaClientBase._resolve": ("testing/client.py", "NinjaClientBase", "_resolve"),
}


def _without_docstring(tokens: list[Any]) -> list[Any]:
    """As ``scrudful.ninja._without_docstring``."""
    body = next(
        (i + 1 for i, token in enumerate(tokens) if token.type == tokenize.INDENT),
        len(tokens),
    )
    end = body
    while end < len(tokens) and tokens[end].type == tokenize.STRING:
        end += 1
    if end == body or end == len(tokens) or tokens[end].type != tokenize.NEWLINE:
        return tokens
    return tokens[:body] + tokens[end + 1 :]


def digest(source: str) -> str:
    """The digest of a method's ``source``, as ``scrudful.ninja`` makes it."""
    source = textwrap.dedent(source)
    tokens = [
        token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in IGNORED
    ]
    sha = hashlib.sha256()
    for token in _without_docstring(tokens):
        text = "" if token.type in STRUCTURE else token.string
        sha.update(f"{tokenize.tok_name[token.type]}\0{text}\1".encode())
    return sha.hexdigest()


def method_source(text: str, owner: str, name: str) -> str | None:
    """The source of method ``name`` of class ``owner`` in module ``text``, as
    ``inspect.getsource`` gives it (its decorators included)."""
    lines = text.splitlines(keepends=True)
    for node in ast.parse(text).body:
        if isinstance(node, ast.ClassDef) and node.name == owner:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == name
                ):
                    start = min([d.lineno for d in item.decorator_list] + [item.lineno])
                    return "".join(lines[start - 1 : item.end_lineno])
    return None


def method_code(text: str, path: str, owner: str, name: str) -> Any:
    """``(co_names, co_varnames, co_argcount)`` of the method, compiled (not
    run) by the Python running this script."""
    module = compile(text, path, "exec")
    for owner_code in module.co_consts:
        if getattr(owner_code, "co_name", None) == owner:
            for code in owner_code.co_consts:
                if getattr(code, "co_name", None) == name:
                    return code.co_names, code.co_varnames, code.co_argcount
    return None


def digests(root: pathlib.Path, with_code: bool = False) -> dict[str, Any]:
    """Each target's digest (``None`` if missing), and its code with
    ``with_code``, for the ``ninja`` package in ``root``."""
    result: dict[str, Any] = {}
    for key, (module, owner, name) in TARGETS.items():
        path = root / "ninja" / module
        text = path.read_text()
        source = method_source(text, owner, name)
        value: Any = None if source is None else digest(source)
        if with_code:
            value = (value, method_code(text, str(path), owner, name))
        result[key] = value
    return result


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    for key, value in digests(pathlib.Path(argv[0]), "--code" in argv).items():
        if isinstance(value, tuple):
            print(key, value[0] or "MISSING", value[1])
        else:
            print(key, value or "MISSING")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
