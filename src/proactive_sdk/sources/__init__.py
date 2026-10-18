"""Public source connectors (W4).

Hosts register these classes on ``ProactiveAgent.registry`` from their own
app module — the v2 decision is programmatic registration, with no
config-file schema extension. A connector that is never registered never
creates a network surface (``EgressBroker`` is built inside it).
"""

from __future__ import annotations

from .rss import RssSource
from .sitemap_diff import SitemapDiffSource

__all__ = ["RssSource", "SitemapDiffSource"]
