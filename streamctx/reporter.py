"""Rich terminal reporting for streamctx sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import pricing

if TYPE_CHECKING:
    from .tracker import LLMTracker

console = Console()


def _format_dollars(amount: float) -> str:
    return f"${amount:.2f}"


def _compute_savings(stats: dict[str, Any], model: str | None = None) -> float:
    reused = int(stats.get("reused_tokens", 0))
    if reused <= 0:
        return 0.0
    return pricing.estimate_savings(model, reused)


def _cache_pct(stats: dict[str, Any]) -> int:
    total = int(stats.get("input_tokens", 0)) + int(stats.get("reused_tokens", 0))
    if total == 0:
        return 0
    return int(round(100 * int(stats.get("reused_tokens", 0)) / total))


def _resolve_waste(stats: dict[str, Any], tracker: "LLMTracker") -> str:
    waste = stats.get("biggest_waste")
    if waste:
        return waste
    if tracker.state.waste_counter:
        return tracker.state.waste_counter.most_common(1)[0][0]
    if tracker.diff.biggest_waste():
        return tracker.diff.biggest_waste()
    return "repeated system prompt"


def print_auto_summary(tracker: "LLMTracker") -> None:
    """Print the compact auto-summary after the first LLM call."""
    stats = tracker.get_stats()
    calls = stats["call_count"]
    tokens = stats["total_tokens"]
    cost = stats["total_cost"]
    cache_pct = _cache_pct(stats)
    savings = _compute_savings(stats)
    waste = _resolve_waste(stats, tracker)
    could_save = savings if savings > 0 else cost * 0.25

    line = "_" * 115
    console.print(line, style="dim")
    console.print(
        f"streamctx :{calls} calls | {tokens} tokens | {_format_dollars(cost)} estimated"
    )
    console.print(
        f"streamctx : {cache_pct}% cached/reused context | saving : {_format_dollars(savings)}"
    )
    console.print(f"streamctx : biggest waste: {waste}")
    console.print(
        f"streamctx : you could have saved {_format_dollars(could_save)} (estimated)",
        style="bold green",
    )
    console.print(line, style="dim")


def print_report(tracker: "LLMTracker") -> None:
    """Print a full Rich summary of the current session."""
    stats = tracker.get_stats()
    calls = stats["call_count"]
    tokens = stats["total_tokens"]
    cost = stats["total_cost"]
    cache_pct = _cache_pct(stats)
    savings = _compute_savings(stats)
    waste = _resolve_waste(stats, tracker)
    could_save = max(savings, cost * 0.25) if cost > 0 else savings

    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Calls", str(calls))
    table.add_row("Total tokens", f"{tokens:,}")
    table.add_row("Estimated cost", _format_dollars(cost))
    table.add_row("Cached / reused", f"{cache_pct}%")
    table.add_row("Context savings", _format_dollars(savings))
    table.add_row("Biggest waste", waste)
    table.add_row(
        "You could have saved",
        Text(_format_dollars(could_save) + " (estimated)", style="bold green"),
    )

    panel = Panel(
        table,
        title="[bold]streamctx session report[/bold]",
        border_style="bright_blue",
        padding=(1, 2),
    )
    console.print(panel)

    pricing_table = Table(show_header=True, header_style="bold magenta")
    pricing_table.add_column("Model")
    pricing_table.add_column("Input / 1M", justify="right")
    pricing_table.add_column("Output / 1M", justify="right")

    seen: set[str] = set()
    for key, p in pricing.PRICING_TABLE.items():
        if p.display_name in seen:
            continue
        seen.add(p.display_name)
        pricing_table.add_row(
            p.display_name,
            _format_dollars(p.input_per_million),
            _format_dollars(p.output_per_million),
        )

    console.print(
        Panel(pricing_table, title="Supported pricing", border_style="dim")
    )
