"""Fallback-chain web provider with weighted primary dispatch.

Chain (from ``web.chain``, default ``firecrawl -> tavily -> searxng``):

* The **first two** entries are primaries sharing live traffic ~2/3 : 1/3
  (firecrawl takes the bulk). Assignment rotates per request through the
  weighted ring ``[firecrawl, firecrawl, tavily]`` — ``itertools.cycle.__next__``
  is atomic in CPython, so a burst of parallel ``web_search`` calls spreads
  across both providers instead of hammering one.
* A request whose ring pick fails (error, empty result, or not
  registered/available) falls to the other primary, then to the remaining
  chain tail (searxng) as the last resort. When *both* primaries are
  unavailable (missing keys, network down) every request goes straight to
  searxng.

Config::

    web:
      backend: fallback-chain
      chain: '["firecrawl","tavily","searxng"]'   # JSON string or real list

Only the first two chain entries get the 2:1 split; everything after them is
tried in order once both primaries fail. Entries with no registered provider
(e.g. ``scrapling``) are skipped.
"""

from __future__ import annotations

import itertools
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from plugins.web._common import BaseWebSearchProvider, extract_fail

logger = logging.getLogger(__name__)

_DEFAULT_CHAIN = ["firecrawl", "tavily", "searxng"]
# firecrawl : tavily = 2 : 1
_PRIMARY_WEIGHTS = (2, 1)


def _chain() -> List[str]:
    from hermes_cli.config import load_config_readonly

    try:
        raw = load_config_readonly().get("web", {}).get("chain")
    except Exception as exc:  # noqa: BLE001 — config must never break the walk
        logger.debug("fallback-chain: could not read web.chain: %s", exc)
        raw = None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw:
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = [seg.strip() for seg in raw.split(",") if seg.strip()]
    if isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if str(n).strip()]
        return names or list(_DEFAULT_CHAIN)
    return list(_DEFAULT_CHAIN)


class FallbackChainProvider(BaseWebSearchProvider):
    """Weighted dispatcher over ``web.chain`` with per-request fallback."""

    NAME = "fallback-chain"
    DISPLAY_NAME = "Fallback chain"
    KEY_ENV = ""

    def __init__(self) -> None:
        self._ring: Optional[itertools.cycle] = None
        self._ring_key: Tuple[str, ...] = ()

    # -- registry plumbing -------------------------------------------------
    def _registry_snapshot(self) -> Dict[str, Any]:
        from agent.web_search_registry import _registry

        return _registry.merged()

    def _usable(self, name: str, snapshot: Dict[str, Any], capability: str) -> bool:
        """Registered + capable for this capability + is_available() says up."""
        from agent.provider_registry import is_available_safe

        provider = snapshot.get(name)
        if provider is None:
            logger.info("fallback-chain: '%s' not registered — skipped", name)
            return False
        if capability == "search" and not provider.supports_search():
            return False
        if capability == "extract" and not provider.supports_extract():
            return False
        if not is_available_safe(provider, logger, "provider %s.is_available() raised %%s" % name):
            logger.info("fallback-chain: '%s' not available (key/network) — skipped", name)
            return False
        return True

    def _primaries(self, capability: str, snapshot: Dict[str, Any]) -> List[str]:
        """First two chain entries that are actually usable."""
        return [n for n in _chain()[:2] if self._usable(n, snapshot, capability)]

    def _attempt_order(self, capability: str, snapshot: Dict[str, Any]) -> Tuple[List[str], Optional[str]]:
        """Order to try this request: ring-picked primary, other primary, tail.

        Returns ``(names, picked)`` where ``picked`` is the ring-selected
        primary (None when the ring degenerates to tail-only).
        """
        primaries = self._primaries(capability, snapshot)
        chain = _chain()
        tail = [n for n in chain[2:] if n not in primaries]
        if not primaries:
            return tail, None
        if len(primaries) == 1:
            return primaries + tail, primaries[0]
        # Rebuild the ring whenever the usable primary set changes.
        ring_key = tuple(primaries)
        if ring_key != self._ring_key or self._ring is None:
            self._ring_key = ring_key
            pattern: List[str] = []
            for name, weight in zip(primaries, _PRIMARY_WEIGHTS):
                pattern.extend([name] * weight)
            self._ring = itertools.cycle(pattern)
        picked = next(self._ring)
        others = [p for p in primaries if p != picked]
        return [picked] + others + tail, picked

    def supports_search(self) -> bool:
        snapshot = self._registry_snapshot()
        return bool(self._primaries("search", snapshot)) or any(
            self._usable(n, snapshot, "search") for n in _chain()[2:]
        )

    def supports_extract(self) -> bool:
        snapshot = self._registry_snapshot()
        return bool(self._primaries("extract", snapshot)) or any(
            self._usable(n, snapshot, "extract") for n in _chain()[2:]
        )

    def is_available(self) -> bool:
        return True

    # -- capability dispatch -------------------------------------------------
    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        snapshot = self._registry_snapshot()
        order, picked = self._attempt_order("search", snapshot)
        if picked:
            logger.info("fallback-chain: primary for this request: '%s'", picked)
        errors: List[str] = []
        for name in order:
            provider = snapshot.get(name)
            if provider is None:
                continue
            logger.info("fallback-chain: trying search via '%s': '%s'", name, query)
            try:
                result = provider.search(query, limit=limit)
            except Exception as exc:  # noqa: BLE001 — one bad provider must not kill the chain
                logger.warning("fallback-chain: search via '%s' raised %s: %s", name, type(exc).__name__, exc)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(result, dict) and result.get("success"):
                logger.info("fallback-chain: search succeeded via '%s'", name)
                return result
            err = (result or {}).get("error", "unknown error") if isinstance(result, dict) else str(result)
            logger.warning("fallback-chain: search via '%s' failed: %s", name, err)
            errors.append(f"{name}: {err}")
        return {"success": False, "error": "all chain providers failed (" + "; ".join(errors or order) + ")"}

    async def extract(self, urls: List[str], **kwargs: Any):
        snapshot = self._registry_snapshot()
        order, picked = self._attempt_order("extract", snapshot)
        if picked:
            logger.info("fallback-chain: primary for this request: '%s'", picked)
        errors: List[str] = []
        for name in order:
            provider = snapshot.get(name)
            if provider is None:
                continue
            logger.info("fallback-chain: trying extract via '%s': %d URL(s)", name, len(urls))
            try:
                result = provider.extract(urls, **kwargs)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception as exc:  # noqa: BLE001
                logger.warning("fallback-chain: extract via '%s' raised %s: %s", name, type(exc).__name__, exc)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(result, list) and result and any(isinstance(d, dict) and d.get("content") for d in result):
                logger.info("fallback-chain: extract succeeded via '%s'", name)
                return result
            logger.warning("fallback-chain: extract via '%s' returned no content", name)
            errors.append(f"{name}: no content")
        return extract_fail(urls, "all chain providers failed (" + "; ".join(errors or order) + ")")

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.DISPLAY_NAME,
            "badge": "custom",
            "tag": "Meta-provider",
            "description": "Weighted dispatch: first two web.chain entries split traffic 2:1, tail is last resort.",
            "key_env": "",
            "prompt": "",
            "url": "",
        }
