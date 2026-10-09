"""Fallback-chain web provider plugin — weighted primary dispatch over web.chain."""
from __future__ import annotations
from .provider import FallbackChainProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(FallbackChainProvider())
