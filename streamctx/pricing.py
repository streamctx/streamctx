"""Token pricing table for supported LLM models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float
    display_name: str


# Prices in USD per 1M tokens (approximate public rates).
PRICING_TABLE: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing(2.50, 10.00, "GPT-4o"),
    "gpt-4o-mini": ModelPricing(0.15, 0.60, "GPT-4o Mini"),
    "gpt-3.5-turbo": ModelPricing(0.50, 1.50, "GPT-3.5"),
    "gpt-3.5": ModelPricing(0.50, 1.50, "GPT-3.5"),
    "claude-sonnet-4-20250514": ModelPricing(3.00, 15.00, "Claude Sonnet"),
    "claude-3-5-sonnet-20241022": ModelPricing(3.00, 15.00, "Claude Sonnet"),
    "claude-3-5-sonnet": ModelPricing(3.00, 15.00, "Claude Sonnet"),
    "claude-sonnet": ModelPricing(3.00, 15.00, "Claude Sonnet"),
    "claude-3-haiku-20240307": ModelPricing(0.25, 1.25, "Claude Haiku"),
    "claude-haiku": ModelPricing(0.25, 1.25, "Claude Haiku"),
    "claude-3-haiku": ModelPricing(0.25, 1.25, "Claude Haiku"),
}

DEFAULT_PRICING = ModelPricing(2.50, 10.00, "Unknown model")


def _normalize_model(model: str) -> str:
    return model.lower().strip()


def resolve_pricing(model: Optional[str]) -> ModelPricing:
    if not model:
        return DEFAULT_PRICING
    key = _normalize_model(model)
    if key in PRICING_TABLE:
        return PRICING_TABLE[key]
    for pattern, pricing in PRICING_TABLE.items():
        if pattern in key or key in pattern:
            return pricing
    if "haiku" in key:
        return PRICING_TABLE["claude-haiku"]
    if "sonnet" in key:
        return PRICING_TABLE["claude-sonnet"]
    if "gpt-3.5" in key or "gpt-35" in key:
        return PRICING_TABLE["gpt-3.5-turbo"]
    if "gpt-4o" in key:
        return PRICING_TABLE["gpt-4o"]
    return DEFAULT_PRICING


def estimate_cost(model: Optional[str], input_tokens: int, output_tokens: int) -> float:
    pricing = resolve_pricing(model)
    input_cost = (input_tokens / 1_000_000) * pricing.input_per_million
    output_cost = (output_tokens / 1_000_000) * pricing.output_per_million
    return input_cost + output_cost


def estimate_savings(model: Optional[str], reused_tokens: int) -> float:
    """Estimate savings from reusing cached/repeated context (input-token rate only)."""
    pricing = resolve_pricing(model)
    return (reused_tokens / 1_000_000) * pricing.input_per_million
