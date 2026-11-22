"""Extract Python symbols and resolve calls in two passes using Tree-sitter."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from codelens.graph.models import Edge, Symbol, SymbolGraph, SymbolKind

_PY_LANGUAGE = Language(tspython.language())

# Directories we never descend into when walking a repo for .py files.
_SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".codelens",
}


@dataclass
class _PendingCall:
    """A call site discovered during the first pass, awaiting target resolution."""

    source_id: str
    target_expr: str
    """The call target as written, e.g. 'add' or 'self.compute'."""
    line: int
    self_class_id: str | None
    """Id of the class Symbol that 'self' refers to at the call site, if any."""
    attr_types: dict[str, str] | None = None
    """The self_class_id's attr_types dict (see _IndexAccumulator.attr_types),
    captured at the time this call was recorded, so _resolve_call_target can
    look up inferred attribute types without needing the full accumulator.
    None if self_class_id is None (there is no 'self' context) or if the
    class has no recorded attribute types.
    """


@dataclass
class _IndexAccumulator:
    symbols: dict[str, Symbol] = field(default_factory=dict)
    pending_calls: list[_PendingCall] = field(default_factory=list)
    files_indexed: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    attr_types: dict[str, dict[str, str]] = field(default_factory=dict)
    """Maps class Symbol.id -> {attr_name: inferred_type}.

    inferred_type is either another class Symbol.id (repo class), or one of
    the reserved sentinel strings "dict" / "list". Populated by scanning
    each class's __init__ method during the existing tree walk in
    _walk_module; consumed by _resolve_call_target during pass 2.
    """
    init_nodes: dict[str, tuple[Node, bytes]] = field(default_factory=dict)
    """Maps class Symbol.id -> (its __init__ function-definition Node, the
    module's source bytes). Retained during the walk so the between-pass
    attribute-type scan never re-locates the node in the tree.
    """


def index_repo(repo_path: str | Path) -> SymbolGraph:
    """Extract symbols and resolve each call against the full repository."""
    root = Path(repo_path).resolve()
    acc = _IndexAccumulator()
    parser = Parser(_PY_LANGUAGE)
    for file_path in sorted(_iter_python_files(root)):
        rel_path = file_path.name if root.is_file() else file_path.relative_to(root).as_posix()
        source = file_path.read_bytes()
        tree = parser.parse(source)
        if tree.root_node.has_error:
            acc.files_skipped.append(rel_path)
            continue
        _walk_module(tree.root_node, source, rel_path, acc)
        acc.files_indexed.append(rel_path)

    _populate_attr_types(acc)
    by_bare_name = _build_bare_name_index(acc.symbols)
    return SymbolGraph(
        root_path=str(root),
        symbols=acc.symbols,
        edges=[_resolve_call_target(call, acc.symbols, by_bare_name) for call in acc.pending_calls],
        files_indexed=acc.files_indexed,
        files_skipped=acc.files_skipped,
    )


def _iter_python_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for directory, subdirectories, filenames in root.walk():
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in _SKIP_DIR_NAMES and not (directory / name).is_junction()
        ]
        for name in filenames:
            path = directory / name
            if path.suffix == ".py" and path.is_file() and path.resolve().is_relative_to(root):
                yield path


def _walk_module(
    root_node: Node,
    source: bytes,
    rel_path: str,
    acc: _IndexAccumulator,
) -> None:
    """Recursively visit a module's AST, recording symbols and pending calls.

    Two different "which class" facts are threaded separately, because they
    have different lifetimes:

    - `class_scope_id`: the class whose body we are *directly* inside, with
      no intervening function. Only used to decide whether a newly found
      `function_definition` is a METHOD (and what its `parent_id` is). Reset
      to None the moment we descend into any function body, since defs
      nested inside a method are not themselves methods of that class.
    - `self_class_id`: the class that `self` refers to in the current
      executable scope. Set when descending into a *method's* body (and
      only then), used purely to resolve `self.foo(...)` call sites.
    """

    def visit(
        node: Node,
        name_stack: list[str],
        class_scope_id: str | None,
        self_class_id: str | None,
        current_symbol_id: str | None,
    ) -> None:
        for child in node.children:
            if child.type == "function_definition":
                _handle_function_def(child, name_stack, class_scope_id)
            elif child.type == "class_definition":
                _handle_class_def(child, name_stack, self_class_id, current_symbol_id)
            elif child.type == "call" and current_symbol_id is not None:
                _handle_call(child, current_symbol_id, self_class_id)
            else:
                visit(child, name_stack, class_scope_id, self_class_id, current_symbol_id)

    def _handle_function_def(
        node: Node,
        name_stack: list[str],
        class_scope_id: str | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source)
        qualified_name = ".".join([*name_stack, func_name])
        symbol_id = f"{rel_path}::{qualified_name}"
        is_method = class_scope_id is not None
        kind = SymbolKind.METHOD if is_method else SymbolKind.FUNCTION
        body = node.child_by_field_name("body")
        symbol = Symbol(
            id=symbol_id,
            name=qualified_name if is_method else func_name,
            qualified_name=qualified_name,
            kind=kind,
            file_path=rel_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            docstring=_extract_docstring(body, source) if body is not None else None,
            parent_id=class_scope_id if is_method else None,
        )
        acc.symbols[symbol_id] = symbol
        if body is not None:
            visit(
                body,
                [*name_stack, func_name],
                None,  # nested defs are not methods, regardless of this function's own kind
                class_scope_id if is_method else None,  # 'self' in this body means this class
                symbol_id,
            )

    def _handle_class_def(
        node: Node,
        name_stack: list[str],
        self_class_id: str | None,
        current_symbol_id: str | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source)
        qualified_name = ".".join([*name_stack, class_name])
        symbol_id = f"{rel_path}::{qualified_name}"
        body = node.child_by_field_name("body")
        symbol = Symbol(
            id=symbol_id,
            name=class_name,
            qualified_name=qualified_name,
            kind=SymbolKind.CLASS,
            file_path=rel_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            docstring=_extract_docstring(body, source) if body is not None else None,
            parent_id=None,
        )
        acc.symbols[symbol_id] = symbol
        # Resolve constructor names after all classes have been collected.
        init_node = _find_init_method(node, source)
        if init_node is not None:
            acc.init_nodes[symbol_id] = (init_node, source)
        if body is not None:
            visit(body, [*name_stack, class_name], symbol_id, self_class_id, current_symbol_id)

    def _handle_call(
        node: Node,
        current_symbol_id: str,
        self_class_id: str | None,
    ) -> None:
        function_node = node.child_by_field_name("function")
        if function_node is not None:
            target_expr = _text(function_node, source)
            acc.pending_calls.append(
                _PendingCall(
                    source_id=current_symbol_id,
                    target_expr=target_expr,
                    line=node.start_point[0] + 1,
                    self_class_id=self_class_id,
                )
            )
        # Still recurse into the call so calls nested in its arguments
        # (e.g. `f(g(x))`) are also captured.
        for child in node.children:
            visit(child, [], None, self_class_id, current_symbol_id)

    visit(root_node, [], None, None, None)


def _extract_docstring(body_node: Node, source: bytes) -> str | None:
    """Return the docstring of a function/class body, if the first statement is one."""
    for child in body_node.children:
        if child.type == "expression_statement":
            string_node = child.children[0] if child.children else None
            if string_node is not None and string_node.type == "string":
                return _string_literal_value(string_node, source)
            return None
        if child.type in ("comment",):
            continue
        return None
    return None


def _string_literal_value(string_node: Node, source: bytes) -> str:
    raw = _text(string_node, source)
    stripped = raw.strip()
    for prefix in ("r", "R", "b", "B", "u", "U", "f", "F", "rb", "Rb", "rB", "RB"):
        if stripped.lower().startswith(prefix.lower()) and len(stripped) > len(prefix):
            stripped = stripped[len(prefix) :]
            break
    for quote in ('"""', "'''", '"', "'"):
        if (
            stripped.startswith(quote)
            and stripped.endswith(quote)
            and len(stripped) >= 2 * len(quote)
        ):
            return stripped[len(quote) : -len(quote)].strip()
    return stripped


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf8")


def _build_bare_name_index(symbols: dict[str, Symbol]) -> dict[str, list[str]]:
    """Map a bare (unqualified) name to every symbol id that could match it.

    Functions are indexed by their plain name. Methods are indexed by the
    last component of their qualified name (e.g. 'Calculator.compute' -> also
    reachable via 'compute'), since call sites rarely spell out the class.
    """
    index: dict[str, list[str]] = {}
    for symbol_id, symbol in symbols.items():
        bare = symbol.qualified_name.split(".")[-1]
        index.setdefault(bare, []).append(symbol_id)
    return index


def _find_init_method(class_node: Node, source: bytes) -> Node | None:
    """Return the class body's direct `__init__` function-definition node, if any.

    Only direct children of the class body count — an `__init__` nested
    inside another method or a conditional block is not the class's
    initializer.

    Args:
        class_node: The `class_definition` node.
        source: The module's raw bytes, for reading node text.

    Returns:
        The `function_definition` node named `__init__`, or None.
    """
    body = class_node.child_by_field_name("body")
    if body is None:
        return None
    for child in body.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node is not None and _text(name_node, source) == "__init__":
                return child
    return None


def _build_known_classes(symbols: dict[str, Symbol]) -> dict[str, str]:
    """Map a bare class name to its Symbol.id, for `self.<attr> = SomeClass()`.

    Built once from the complete symbol set after pass 1 (mirroring how
    `_build_bare_name_index` is built), so an `__init__` in one file can
    construct a class defined in a file walked later. If two classes share a
    bare name the entry is dropped: the constructor call is then ambiguous,
    and this scan refuses to guess rather than pick one at random.
    """
    known: dict[str, str] = {}
    for symbol_id, symbol in symbols.items():
        if symbol.kind is not SymbolKind.CLASS:
            continue
        bare = symbol.qualified_name.split(".")[-1]
        if bare in known and known[bare] != symbol_id:
            del known[bare]
        else:
            known[bare] = symbol_id
    return known


def _scan_init_attr_types(
    class_node: Node,
    class_symbol_id: str,
    source: bytes,
    known_classes: dict[str, str],
) -> dict[str, str]:
    """Infer `self.<attr>` types from the `__init__` body's direct assignments.

    Only statements of the exact form `self.<attr> = <expr>` count, and only
    for three shapes of `<expr>`: a call to a known repo class's constructor,
    a dict literal / `dict(...)`, or a list literal / `list(...)`. Anything
    else leaves the attribute's type unknown — no guessing from type
    annotations, conditional expressions, or unrecognized calls.

    Args:
        class_node: The `__init__` function-definition node, retained during
            the module walk so this scan never re-locates it in the tree.
        class_symbol_id: Id of the enclosing class Symbol. Unused by the scan
            itself (the caller keys the result by it) — present because the
            retained node alone cannot identify the class.
        source: The module's raw bytes, for reading node text.
        known_classes: Bare class name -> Symbol.id, across the whole repo.

    Returns:
        Map of attr_name -> inferred type ("dict" / "list" sentinels, or a
        class Symbol.id). Empty when nothing recognizable is assigned.
    """
    attr_types: dict[str, str] = {}
    body = class_node.child_by_field_name("body")
    if body is None:
        return attr_types
    for statement in body.children:
        # Assignments are wrapped in `expression_statement` nodes.
        if statement.type == "expression_statement" and statement.children:
            statement = statement.children[0]
        if statement.type != "assignment":
            continue
        left = statement.child_by_field_name("left")
        right = statement.child_by_field_name("right")
        if left is None or right is None or left.type != "attribute":
            continue
        object_node = left.child_by_field_name("object")
        attr_node = left.child_by_field_name("attribute")
        if object_node is None or attr_node is None or _text(object_node, source) != "self":
            continue
        inferred = _infer_rhs_type(right, source, known_classes)
        if inferred is not None:
            attr_types[_text(attr_node, source)] = inferred
    return attr_types


def _infer_rhs_type(right: Node, source: bytes, known_classes: dict[str, str]) -> str | None:
    """Return the inferred type sentinel for an assignment RHS, or None.

    Recognizes exactly: a call to a known repo class's constructor, a dict
    literal or `dict(...)` call ("dict"), and a list literal or `list(...)`
    call ("list"). Every other shape is unrecognized and yields None.
    """
    if right.type == "dictionary":
        return "dict"
    if right.type == "list":
        return "list"
    if right.type == "call":
        function_node = right.child_by_field_name("function")
        if function_node is not None and function_node.type == "identifier":
            name = _text(function_node, source)
            if name in known_classes:
                return known_classes[name]
            if name == "dict":
                return "dict"
            if name == "list":
                return "list"
    return None


def _populate_attr_types(acc: _IndexAccumulator) -> None:
    """Infer receiver types after collecting all classes, then attach them to calls."""
    known_classes = _build_known_classes(acc.symbols)
    for class_id, (init_node, source) in acc.init_nodes.items():
        scanned = _scan_init_attr_types(init_node, class_id, source, known_classes)
        if scanned:
            acc.attr_types[class_id] = scanned
    for call in acc.pending_calls:
        if call.self_class_id is not None:
            call.attr_types = acc.attr_types.get(call.self_class_id)


def _resolve_call_target(
    call: _PendingCall,
    symbols: dict[str, Symbol],
    by_bare_name: dict[str, list[str]],
) -> Edge:
    """Resolve a pending call's target expression to a symbol id, if possible.

    Resolution order:
      1. `self.<name>` inside a class: look for a method named `<name>` whose
         `parent_id` is the enclosing class. Unambiguous by construction --
         and, one attribute hop deeper, so is `self.<attr>.<name>()` when
         `self.<attr>`'s type is *inferred* from a traceable `__init__`
         assignment (`self._store = Storage()`, a dict literal, or a list
         literal): the method is looked up on that specific class. A receiver
         inferred to be a dict or list (e.g. `self._jobs.get(k)` on
         `self._jobs = {}`) is refused outright rather than guessed at;
         a receiver with no inferred type is not a `self.<name>` call.
      2. Otherwise, treat the last dotted component as a bare name and look
         it up in `by_bare_name`. Resolve only if exactly one symbol matches;
         ambiguous or unknown names are left unresolved.
      3. A *dotted* call on an unknown receiver (`x.foo()`) may only resolve
         to a top-level symbol, never to a method. Nothing here infers the
         type of `x`, so binding `d.get(...)` to some class's `get` method is
         a guess -- and it is wrong far more often than right: on this repo it
         attached 71 dict lookups to a single unrelated method, which then
         dominated `blast_radius`. A missing edge is a documented limitation;
         a fabricated one silently corrupts the question the graph exists to
         answer, so this refuses rather than guesses.

         (A denylist of builtin method names -- get/items/append/... -- would
         recover a handful of genuine `obj.method()` edges this drops. It was
         considered and rejected: an open-ended list of special cases to
         maintain, in exchange for edges that are still guesses.)
    """
    bare_name = call.target_expr.split(".")[-1]
    is_dotted = "." in call.target_expr

    is_own_method_call = (
        call.target_expr.startswith("self.")
        and call.target_expr.count(".") == 1
        and call.self_class_id is not None
    )
    if is_own_method_call:
        for symbol_id, symbol in symbols.items():
            if symbol.parent_id == call.self_class_id and symbol.qualified_name.endswith(
                f".{bare_name}"
            ):
                return Edge(
                    source_id=call.source_id,
                    target_id=symbol_id,
                    target_name=call.target_expr,
                    line=call.line,
                )

    # `self.<attr>.<name>()` with an inferred attribute type (tier 1.5). The
    # receiver's type comes from a traceable `__init__` assignment
    # (`self._store = Storage()` / `= {}` / `= []`), so a method lookup
    # against that specific class is as safe as tier 1 -- no bare-name guess.
    if (
        call.target_expr.startswith("self.")
        and call.target_expr.count(".") == 2
        and call.self_class_id is not None
        and call.attr_types is not None
    ):
        _, attr_name, method_name = call.target_expr.split(".")
        inferred_type = call.attr_types.get(attr_name)
        if inferred_type == "dict" or inferred_type == "list":
            # Positive knowledge that the receiver is a container: refusing
            # to bind `d.get(...)` to some class's `get` method is now a
            # fact, not a guess.
            return Edge(
                source_id=call.source_id,
                target_id=None,
                target_name=call.target_expr,
                line=call.line,
            )
        if inferred_type is not None:
            for symbol_id, symbol in symbols.items():
                if symbol.parent_id == inferred_type and symbol.qualified_name.endswith(
                    f".{method_name}"
                ):
                    return Edge(
                        source_id=call.source_id,
                        target_id=symbol_id,
                        target_name=call.target_expr,
                        line=call.line,
                    )
            # Method not found on the inferred class (e.g. inherited — out of
            # scope for this inference): fall through to the tiers below.

    candidates = by_bare_name.get(bare_name, [])
    target_id = candidates[0] if len(candidates) == 1 else None
    if target_id is not None and is_dotted and symbols[target_id].parent_id is not None:
        target_id = None
    return Edge(
        source_id=call.source_id,
        target_id=target_id,
        target_name=call.target_expr,
        line=call.line,
    )
