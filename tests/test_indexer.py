"""Tests for graph.indexer.index_repo against the sample_repo fixture."""

from pathlib import Path

from codelens.graph.indexer import index_repo
from codelens.graph.models import SymbolKind

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


def test_finds_all_expected_symbols():
    graph = index_repo(FIXTURE_REPO)
    qualified_names = {s.qualified_name for s in graph.symbols.values()}

    assert "add" in qualified_names
    assert "double" in qualified_names
    assert "Calculator" in qualified_names
    assert "Calculator.compute" in qualified_names
    assert "Calculator.compute_twice" in qualified_names
    assert "run" in qualified_names


def test_symbol_kinds_and_parent_ids():
    graph = index_repo(FIXTURE_REPO)
    by_qname = {s.qualified_name: s for s in graph.symbols.values()}

    assert by_qname["add"].kind == SymbolKind.FUNCTION
    assert by_qname["add"].parent_id is None

    assert by_qname["Calculator"].kind == SymbolKind.CLASS

    compute = by_qname["Calculator.compute"]
    assert compute.kind == SymbolKind.METHOD
    assert compute.parent_id == by_qname["Calculator"].id


def test_symbol_line_ranges_are_sane():
    graph = index_repo(FIXTURE_REPO)
    by_qname = {s.qualified_name: s for s in graph.symbols.values()}
    add = by_qname["add"]
    assert add.line_start >= 1
    assert add.line_end >= add.line_start


def test_docstrings_extracted():
    graph = index_repo(FIXTURE_REPO)
    by_qname = {s.qualified_name: s for s in graph.symbols.values()}
    assert by_qname["add"].docstring == "Return the sum of two integers."
    assert by_qname["Calculator"].docstring == "Performs a couple of toy arithmetic operations."


def test_resolves_plain_call_edges():
    graph = index_repo(FIXTURE_REPO)
    by_qname = {s.qualified_name: s for s in graph.symbols.values()}
    add_id = by_qname["add"].id
    double_id = by_qname["double"].id

    double_targets = {e.target_id for e in graph.edges if e.source_id == double_id}
    assert add_id in double_targets


def test_resolves_self_call_edges():
    graph = index_repo(FIXTURE_REPO)
    by_qname = {s.qualified_name: s for s in graph.symbols.values()}
    compute_id = by_qname["Calculator.compute"].id
    compute_twice_id = by_qname["Calculator.compute_twice"].id

    compute_twice_targets = {e.target_id for e in graph.edges if e.source_id == compute_twice_id}
    assert compute_id in compute_twice_targets


def test_resolves_cross_file_call_edges():
    graph = index_repo(FIXTURE_REPO)
    by_qname = {s.qualified_name: s for s in graph.symbols.values()}
    add_id = by_qname["add"].id
    double_id = by_qname["double"].id
    compute_id = by_qname["Calculator.compute"].id

    compute_targets = {e.target_id for e in graph.edges if e.source_id == compute_id}
    assert add_id in compute_targets
    assert double_id in compute_targets


def test_files_indexed_and_none_skipped():
    graph = index_repo(FIXTURE_REPO)
    assert "sample_pkg/utils.py" in graph.files_indexed
    assert "sample_pkg/service.py" in graph.files_indexed
    assert "sample_pkg/main.py" in graph.files_indexed
    assert graph.files_skipped == []


def test_index_single_file():
    graph = index_repo(FIXTURE_REPO / "sample_pkg" / "utils.py")
    qualified_names = {s.qualified_name for s in graph.symbols.values()}
    assert qualified_names == {"add", "double"}


def test_dotted_call_does_not_bind_to_an_unrelated_method(tmp_path):
    """`d.get(...)` must not resolve to some class's `get` method.

    Nothing here infers the type of `d`, so binding them is a guess. It used
    to happen whenever exactly one symbol had a matching bare name, which on
    a real repo attached 71 dict lookups to a single method and made it the
    top result in "most referenced" -- and put it in everything's blast
    radius. An unresolved edge is honest; a fabricated one is not.
    """
    (tmp_path / "m.py").write_text(
        "class Cache:\n"
        "    def get(self, key):\n"
        "        return key\n"
        "\n"
        "def lookup(mapping):\n"
        "    return mapping.get('k')\n"
    )

    graph = index_repo(tmp_path)

    lookup_edges = [e for e in graph.edges if e.source_id.endswith("::lookup")]
    assert len(lookup_edges) == 1
    assert lookup_edges[0].target_name == "mapping.get"
    assert lookup_edges[0].target_id is None, "must stay unresolved rather than guess"


def test_dotted_call_still_resolves_to_a_module_level_function(tmp_path):
    """The tightened rule must not throw away `module.function()` edges,
    which are legitimate and common."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "helpers.py").write_text("def transform(value):\n    return value\n")
    (pkg / "main.py").write_text("import helpers\n\ndef run(v):\n    return helpers.transform(v)\n")

    graph = index_repo(tmp_path)

    edge = next(e for e in graph.edges if e.target_name == "helpers.transform")
    assert edge.target_id is not None
    assert graph.symbols[edge.target_id].qualified_name == "transform"


def test_self_method_call_still_resolves(tmp_path):
    """`self.x()` is unambiguous by construction and must keep resolving."""
    (tmp_path / "m.py").write_text(
        "class Thing:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def run(self):\n"
        "        return self.helper()\n"
    )

    graph = index_repo(tmp_path)

    edge = next(e for e in graph.edges if e.target_name == "self.helper")
    assert graph.symbols[edge.target_id].qualified_name == "Thing.helper"


def test_plain_call_to_a_unique_function_still_resolves(tmp_path):
    (tmp_path / "m.py").write_text(
        "def helper():\n    return 1\n\ndef run():\n    return helper()\n"
    )

    graph = index_repo(tmp_path)

    edge = next(e for e in graph.edges if e.target_name == "helper")
    assert graph.symbols[edge.target_id].qualified_name == "helper"


def test_call_through_a_self_attribute_is_not_a_call_on_self(tmp_path):
    """`self._jobs.get(k)` is a dict lookup, not the class's own `get`.

    The self-rule matched any target starting with "self.", so a call on an
    *attribute* of self was bound to a same-named method of the enclosing
    class. That fabricated self-recursion: the graph claimed `JobRunner.get`
    called itself, which then drew as a real-looking bidirectional edge in
    the neighbourhood view.
    """
    (tmp_path / "m.py").write_text(
        "class Runner:\n"
        "    def __init__(self):\n"
        "        self._jobs = {}\n"
        "\n"
        "    def get(self, key):\n"
        "        return self._jobs.get(key)\n"
    )

    graph = index_repo(tmp_path)

    lookup = next(e for e in graph.edges if e.target_name == "self._jobs.get")
    assert lookup.target_id is None, "must not resolve to Runner.get"


def test_a_plain_self_method_call_still_resolves(tmp_path):
    """Narrowing the rule must not break the case it exists for."""
    (tmp_path / "m.py").write_text(
        "class Runner:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def run(self):\n"
        "        return self.helper()\n"
    )

    graph = index_repo(tmp_path)

    edge = next(e for e in graph.edges if e.target_name == "self.helper")
    assert graph.symbols[edge.target_id].qualified_name == "Runner.helper"


def test_genuine_self_recursion_is_still_recorded(tmp_path):
    """A function that really does call itself keeps that edge — the graph
    view draws it as a loop, and it is true."""
    (tmp_path / "m.py").write_text("def walk(node):\n    return walk(node)\n")

    graph = index_repo(tmp_path)

    edge = next(e for e in graph.edges if e.target_name == "walk")
    assert edge.source_id == edge.target_id


def test_self_attr_call_resolves_to_inferred_class_method(tmp_path):
    """B.5.1 — `self._store.get(k)` resolves to `Storage.get`, not any other `get`."""
    (tmp_path / "m.py").write_text(
        "class Storage:\n"
        "    def get(self, key):\n"
        "        return key\n"
        "\n"
        "class Other:\n"
        "    def get(self, key):\n"
        "        return key\n"
        "\n"
        "def get(key):\n"
        "    return key\n"
        "\n"
        "class Cache:\n"
        "    def __init__(self):\n"
        "        self._store = Storage()\n"
        "\n"
        "    def read(self, key):\n"
        "        return self._store.get(key)\n"
    )

    graph = index_repo(tmp_path)
    by_qname = {s.qualified_name: s for s in graph.symbols.values()}
    storage_get_id = by_qname["Storage.get"].id

    edge = next(e for e in graph.edges if e.target_name == "self._store.get")
    assert edge.target_id == storage_get_id
    assert edge.target_id != by_qname["Other.get"].id


def test_self_attr_call_on_inferred_dict_stays_unresolved(tmp_path):
    """B.5.2 — `self._data = {}` makes `self._data.get(k)` a refused dict call."""
    (tmp_path / "m.py").write_text(
        "class Cache:\n"
        "    def __init__(self):\n"
        "        self._data = {}\n"
        "\n"
        "    def get(self, key):\n"
        "        return self._data.get(key)\n"
    )

    graph = index_repo(tmp_path)

    edge = next(e for e in graph.edges if e.target_name == "self._data.get")
    assert edge.target_id is None, "must not resolve to Cache.get"


def test_self_attr_call_with_unrecognized_rhs_stays_unresolved(tmp_path):
    """B.5.3 — unrecognized RHS -> no inferred type -> no guess, still unresolved."""
    (tmp_path / "m.py").write_text(
        "def compute_default():\n"
        "    return {}\n"
        "\n"
        "class Cache:\n"
        "    def __init__(self):\n"
        "        self._data = compute_default()\n"
        "\n"
        "    def get(self, key):\n"
        "        return self._data.get(key)\n"
    )

    graph = index_repo(tmp_path)

    edge = next(e for e in graph.edges if e.target_name == "self._data.get")
    assert edge.target_id is None
