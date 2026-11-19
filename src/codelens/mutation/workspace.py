"""Freeze Git's working tree and bind expression edits to specific syntax locations."""

import ast
import difflib
import hashlib
import os
import re
import stat
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from codelens.graph.indexer import index_repo
from codelens.review.changes import files_to_copy, git, is_test_file

from .models import Proposal, Target


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(name: str) -> str:
    if (
        not name
        or "\\" in name
        or ":" in name
        or name.startswith(("/", "-", "@"))
        or any(ord(ch) < 32 for ch in name)
        or any(
            p in {"", ".", "..", ".git"}
            or p.rstrip(" .") != p
            or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
            for p in name.split("/")
        )
    ):
        raise ValueError(f"Expected a repository-relative path: {name!r}")
    return name


def snapshot(root: Path, dest: Path | None = None) -> str:
    manifest = hashlib.sha256()
    total = 0
    names = files_to_copy(root)
    if len(names) > 10000:
        raise ValueError("Workspace exceeds 10000 files")
    for name in names:
        if any(
            p in {".codelens", ".git", ".venv", "__pycache__", ".pytest_cache"}
            for p in name.split("/")
        ):
            continue
        path = root / relative(name)
        for component in (path, *path.parents):
            if component == root:
                break
            if component.is_symlink() or component.is_junction():
                raise ValueError(f"Linked paths are unsupported: {name}")
        if not path.exists():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024**2:
            raise ValueError(f"Unsupported file type or size: {name}")
        total += info.st_size
        if total > 48 * 1024**2:
            raise ValueError("Workspace exceeds 48 MiB")
        data = path.read_bytes()
        executable = os.name != "nt" and bool(info.st_mode & 0o111)
        manifest.update(name.encode() + b"\0" + bytes([executable]) + bytes.fromhex(digest(data)))
        if dest is not None:
            copy = dest / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(data)
            copy.chmod(0o755 if os.name != "nt" and info.st_mode & 0o111 else 0o644)
    return manifest.hexdigest()


def validate_scope(root: Path, scope: list[str]) -> None:
    for selector in scope:
        file, *ids = selector.split("::")
        if file != ".":
            relative(file)
        if not (root / file).exists() or any(not i or "\n" in i for i in ids):
            raise ValueError(f"Missing test path or malformed node ID: {selector}")


def context(
    root: Path, frozen: Path, focus: list[str], fingerprint: str
) -> tuple[list[Target], str]:
    graph = index_repo(frozen, use_cache=False)
    changed = set(
        git(root, "diff", "--no-ext-diff", "--name-only", "-z", "HEAD", "--").decode().split("\0")
    )
    changed.update(
        git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    )
    symbols = [s for s in graph.symbols.values() if s.kind in {"function", "method"}]
    selected = {s.file_path for s in symbols if (s.file_path in focus or s.id in focus)}
    if focus and any(f not in selected and not any(s.id == f for s in symbols) for f in focus):
        raise ValueError("Unknown focus file or symbol; use a Python function or its file")
    files = sorted(selected if focus else changed)
    targets: list[Target] = []
    patches: list[str] = []
    for filename in files:
        p = Path(filename)
        if (
            p.suffix != ".py"
            or is_test_file(filename)
            or p.name in {"setup.py", "noxfile.py", "fabfile.py"}
            or set(p.parts) & {"scripts", "tools", "build", "ci", ".github"}
            or not (frozen / filename).is_file()
        ):
            continue
        source = (frozen / filename).read_text(encoding="utf-8")
        # Compare HEAD with frozen bytes, including untracked files.
        try:
            previous = git(root, "show", "HEAD:" + filename).decode()
        except ValueError:
            previous = ""
        if sum(map(len, patches)) < 4000:
            patches.append(
                "".join(
                    difflib.unified_diff(
                        previous.splitlines(True),
                        source.splitlines(True),
                        "a/" + filename,
                        "b/" + filename,
                    )
                )[:4000]
            )
        syms = [s for s in symbols if s.file_path == filename]
        tree = ast.parse(source)
        lines = source.splitlines()
        for stmt in ast.walk(tree):
            value = (
                stmt.value
                if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Return))
                else stmt.test
                if isinstance(stmt, ast.If)
                else None
            )
            if value is None or value.end_lineno != value.lineno:
                continue
            assert isinstance(stmt, ast.stmt)
            owners = [s for s in syms if s.line_start <= stmt.lineno <= s.line_end]
            if not owners:
                continue
            symbol = min(owners, key=lambda s: s.line_end - s.line_start)
            if focus and filename not in focus and symbol.id not in focus:
                continue
            anchor = ast.get_source_segment(source, value) or ""
            if not 0 < len(anchor) <= 512:
                continue
            col = len(lines[value.lineno - 1].encode()[: value.col_offset].decode())
            key = digest(f"{fingerprint}:{symbol.id}:{value.lineno}:{col}:{anchor}".encode())
            callers = sorted({e.source_id for e in graph.edges if e.target_id == symbol.id})
            targets.append(
                Target(
                    target_id=key,
                    symbol=symbol.id,
                    file=filename,
                    line=value.lineno,
                    column=col,
                    expected_anchor=anchor,
                    static_callers=callers[:5],
                    excerpt="\n".join(lines[max(0, stmt.lineno - 2) : stmt.lineno + 1])[:750],
                )
            )
            if len(targets) >= 256:
                return targets, "".join(patches)[:4000]
    return targets, "".join(patches)[:4000]


ALLOWED = (
    ast.Expression,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.IfExp,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.Compare,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
    ast.Is,
    ast.IsNot,
    ast.BinOp,
    ast.Add,
    ast.Sub,
)


def edit(source: bytes, target: Target, proposal: Proposal) -> tuple[bytes, str]:
    if proposal.expected_anchor != target.expected_anchor:
        raise ValueError("Stale anchor; copy the exact expected_anchor from get_mutation_run")
    replacement = proposal.replacement
    if "\n" in replacement or "\r" in replacement or "#" in replacement:
        raise ValueError("Use one simple expression on one line")
    try:
        expr = ast.parse(replacement, mode="eval")
        replacement_node = cst.parse_expression(replacement)
    except (SyntaxError, cst.ParserSyntaxError) as exc:
        raise ValueError("Invalid replacement expression") from exc
    functions = [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.lineno <= target.line <= (n.end_lineno or n.lineno)
    ]
    if not functions:
        raise ValueError("Anchor is outside a function")
    owner = min(functions, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
    names = {
        n.id for n in ast.walk(owner) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }
    names.update(n.arg for n in ast.walk(owner) if isinstance(n, ast.arg))
    if any(
        not isinstance(n, ALLOWED) or isinstance(n, ast.Name) and n.id not in names
        for n in ast.walk(expr)
    ):
        raise ValueError(
            "Use local names, literals, comparisons, +/-, boolean or conditional expressions"
        )
    if ast.dump(expr.body) == ast.dump(ast.parse(target.expected_anchor, mode="eval").body):
        raise ValueError("Replacement is identical to the baseline expression")
    module = cst.parse_module(source)

    class Replace(cst.CSTTransformer):
        METADATA_DEPENDENCIES = (PositionProvider,)
        count = 0

        def on_leave(self, original_node, updated_node):
            if isinstance(original_node, cst.BaseExpression):
                start = self.get_metadata(PositionProvider, original_node).start
                if (start.line, start.column) == (
                    target.line,
                    target.column,
                ) and module.code_for_node(original_node).strip() == target.expected_anchor:
                    self.count += 1
                    return replacement_node
            return updated_node

    visitor = Replace()
    mutated = MetadataWrapper(module).visit(visitor).bytes
    if visitor.count != 1:
        raise ValueError(
            "Anchor does not identify one replaceable expression; choose another target"
        )
    ast.parse(mutated)
    patch = "".join(
        difflib.unified_diff(
            source.decode().splitlines(True),
            mutated.decode().splitlines(True),
            "a/" + target.file,
            "b/" + target.file,
            n=0,
        )
    )
    return mutated, patch
