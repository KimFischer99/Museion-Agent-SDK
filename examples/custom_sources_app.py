"""Example: register extra public sources programmatically (v2 W4).

PAS is plugin-first: a host that embeds it wires its sources in its own app
module instead of extending a config-file schema. This is the smallest
complete shape of that wiring.

    pas serve --app examples.custom_sources_app:build_agent --config pas.yaml

Security defaults are enforced by the connector itself: HTTPS only, an
explicit host allowlist, bounded response size/timeouts, redirects refused.
A source that is never registered creates no network surface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # direct-script import support
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import OpenAICompatibleModel, ProactiveAgent  # noqa: E402
from proactive_sdk.sources import PageHashSource, RssSource, SitemapDiffSource  # noqa: E402

#: Edit this list (or build it from your own trusted configuration) to add
#: feeds. ``allowed_hosts`` is mandatory and covers exactly the hosts the
#: feed and its entries may live on.
SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "example-news",
        "url": "https://example.org/feed.xml",
        "account_ref": "account:public",
        "allowed_hosts": ["example.org"],
        "required_capability": "public.read",
    }
]

#: Page sources report a change only when the readable text moved beyond a
#: near-duplicate threshold (ads/nav rotation stays silent).
PAGE_SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "example-page",
        "url": "https://example.org/updates",
        "account_ref": "account:public",
        "allowed_hosts": ["example.org"],
        "required_capability": "public.read",
    }
]

#: Sitemap sources diff only new/changed entries; the first fetch is a
#: baseline (zero items) and nothing is fetched until a job targets them.
SITEMAP_SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "example-sitemap",
        "url": "https://example.org/sitemap.xml",
        "account_ref": "account:public",
        "allowed_hosts": ["example.org"],
        "required_capability": "public.read",
    }
]


def register_public_sources(agent: ProactiveAgent, specs: list[dict[str, Any]]) -> None:
    """Register RSS/Atom sources on an existing agent."""
    for spec in specs:
        agent.registry.register(
            source_id=spec["source_id"],
            account_ref=spec.get("account_ref", "account:public"),
            required_capability=spec.get("required_capability", "public.read"),
            source=RssSource(
                url=spec["url"],
                source_id=spec["source_id"],
                account_ref=spec.get("account_ref", "account:public"),
                allowed_hosts=tuple(spec["allowed_hosts"]),
                required_capability=spec.get("required_capability", "public.read"),
            ),
        )


def register_sitemap_sources(agent: ProactiveAgent, specs: list[dict[str, Any]]) -> None:
    """Register sitemap-diff sources on an existing agent."""
    for spec in specs:
        agent.registry.register(
            source_id=spec["source_id"],
            account_ref=spec.get("account_ref", "account:public"),
            required_capability=spec.get("required_capability", "public.read"),
            source=SitemapDiffSource(
                url=spec["url"],
                source_id=spec["source_id"],
                account_ref=spec.get("account_ref", "account:public"),
                allowed_hosts=tuple(spec["allowed_hosts"]),
                required_capability=spec.get("required_capability", "public.read"),
            ),
        )


def register_page_sources(agent: ProactiveAgent, specs: list[dict[str, Any]]) -> None:
    """Register page-hash sources on an existing agent."""
    for spec in specs:
        agent.registry.register(
            source_id=spec["source_id"],
            account_ref=spec.get("account_ref", "account:public"),
            required_capability=spec.get("required_capability", "public.read"),
            source=PageHashSource(
                url=spec["url"],
                source_id=spec["source_id"],
                account_ref=spec.get("account_ref", "account:public"),
                allowed_hosts=tuple(spec["allowed_hosts"]),
                required_capability=spec.get("required_capability", "public.read"),
            ),
        )


def build_agent(config: Any = None) -> ProactiveAgent:
    """App module for ``pas serve --app examples.custom_sources_app:build_agent``."""
    if config is None:
        raise SystemExit("this example expects --config; see README")
    state_dir = Path(os.environ.get("PAS_STATE_DIR", config.state_dir)).expanduser()
    model = OpenAICompatibleModel(
        base_url=os.environ["PAS_MODEL_BASE_URL"],
        api_key_provider=lambda: os.environ["PAS_MODEL_API_KEY"],
        model=os.environ["PAS_MODEL_NAME"],
    )
    agent = ProactiveAgent(
        state_dir=state_dir,
        model=model,
        config=config,
        timezone=config.timezone,
        profile=config.profile,
        locale=config.locale,
    )
    register_public_sources(agent, SOURCES)
    register_sitemap_sources(agent, SITEMAP_SOURCES)
    register_page_sources(agent, PAGE_SOURCES)
    return agent
