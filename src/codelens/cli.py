"""Command-line entry points for graph inspection and patch review."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click

from codelens.graph.indexer import index_repo
from codelens.graph.models import SymbolGraph
from codelens.graph.tree_export import export_tree_html


@click.group()
def cli() -> None:
    """CodeLens: Python impact analysis, patch review, and mutation testing."""


@cli.command(name="index")
@click.argument("repo_path", type=click.Path(exists=True, file_okay=False))
def index_cmd(repo_path: str) -> None:
    """Index a Python repository and print symbol and call counts."""
    click.echo(_summary_line(repo_path, index_repo(repo_path)))


@cli.command(name="review")
@click.option("--base", required=True, help="Git commit/ref to compare with the working tree.")
@click.option("--repo", type=click.Path(exists=True, file_okay=False), default=".")
@click.option(
    "--test-path",
    multiple=True,
    default=("tests",),
    help="Pytest path/node ID inside the repo; repeat to specify several.",
)
@click.option("--output", type=click.Path(file_okay=False), default=None)
@click.option("--max-mutations", type=click.IntRange(0, 100), default=5, show_default=True)
@click.option(
    "--timeout",
    type=click.IntRange(1, 600),
    default=60,
    show_default=True,
    help="Seconds per pytest process, including baseline and controls.",
)
@click.option("--fail-on-survivor", is_flag=True, help="Exit 1 when a mutation survives.")
def review_cmd(base, repo, test_path, output, max_mutations, timeout, fail_on_survivor) -> None:
    """Review a Python patch using actual pytest execution and bounded mutations.

    Executes trusted repository tests in disposable source copies. Requires the
    project's test dependencies in this interpreter. Does not edit source files.
    """
    import subprocess
    from collections import Counter

    from codelens.review.engine import review

    root = Path(repo).resolve()
    destination = Path(output).resolve() if output else root / ".codelens" / "review"
    click.echo("Reviewing patch; running trusted pytest code in disposable copies...")
    try:
        report = review(
            root, base, destination, list(test_path), max_mutations=max_mutations, timeout=timeout
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc
    counts = Counter(m.status for m in report.mutations)
    baseline_status = report.baseline.status if report.baseline is not None else "unavailable"
    click.echo(f"Review: {report.status}; baseline: {baseline_status}")
    click.echo(f"Changed functions: {len(report.functions)}; mutations: {dict(counts)}")
    click.echo(f"HTML: {destination / 'report.html'}")
    click.echo(f"JSON: {destination / 'report.json'}")
    click.echo("Coverage is execution evidence. Survivors need review; this is not a safe verdict.")
    if report.status == "baseline_failed":
        raise click.exceptions.Exit(2)
    if fail_on_survivor and counts["survived"]:
        raise click.exceptions.Exit(1)


@cli.command(name="review-demo")
@click.option(
    "--output",
    type=click.Path(file_okay=False),
    required=True,
    help="New directory for two synthetic repos and their reports.",
)
def review_demo_cmd(output: str) -> None:
    """Show a covered mutation surviving weak tests, then detected by a stronger test."""
    from codelens.review.demo import create_demo

    destination = Path(output).resolve()
    click.echo("Creating synthetic discount examples; running real pytest mutation checks...")
    try:
        reports = create_demo(destination)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    for name, report in reports.items():
        baseline_status = report.baseline.status if report.baseline is not None else "unavailable"
        click.echo(
            f"{name}: baseline {baseline_status}; mutations {[m.status for m in report.mutations]}"
        )
        click.echo(f"  {destination / name / '.codelens/review/report.html'}")


def _summary_line(repo_path: str, graph: SymbolGraph) -> str:
    """One-line index summary, shared by `index` and `tree` so they can't drift."""
    resolved = sum(1 for edge in graph.edges if edge.target_id is not None)
    return (
        f"Indexed {repo_path}: {len(graph.symbols)} symbols, {len(graph.edges)} edges "
        f"({resolved} resolved, {len(graph.edges) - resolved} unresolved), "
        f"{len(graph.files_indexed)} files indexed, {len(graph.files_skipped)} skipped"
    )


@cli.command(name="tree")
@click.argument("repo_path", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Where to write the HTML file. Defaults to ./logs/tree-<timestamp>.html",
)
def tree_cmd(repo_path: str, output: str | None) -> None:
    """Index REPO_PATH and write a standalone HTML dependency tree.

    The output is a single self-contained file — open it by double-clicking,
    no server needed.

    Example:

        codelens tree ./my-repo
    """
    if output is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = str(Path("logs") / f"tree-{timestamp}.html")

    graph = index_repo(repo_path)
    written = export_tree_html(graph, output)

    click.echo(_summary_line(repo_path, graph))
    click.echo(f"\nWrote tree to: {written}")
    click.echo(f"Open it: {written.as_uri()}")


def main() -> None:
    """Console-script entrypoint (see [project.scripts] in pyproject.toml)."""
    cli()


if __name__ == "__main__":
    main()
