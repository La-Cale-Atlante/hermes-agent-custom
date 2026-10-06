"""axis-github plugin — additive GitHub tool authenticated by the Axis connectors service.

Registers a single tool (`github_api`) ON TOP of the built-in toolset — it never overrides or
removes anything (narrow-waist doctrine: capability at the edges). The tool is gated by
`check_github_requirements`, so it only surfaces to the model when the Axis platform has provisioned
the connector for the run (env injected by the Timonier). The auth (GitHub App, secret store, token
minting) lives entirely in the Axis platform; the sandbox holds no credential.
"""

from __future__ import annotations

import logging

from plugins.axis_github.tools import (
    GITHUB_API_SCHEMA,
    check_github_requirements,
    handle_github_api,
)

logger = logging.getLogger("axis.github")


def register(ctx) -> None:
    """Register the `github_api` tool. Called once by the plugin loader when enabled.

    `check_fn` keeps the tool additive-and-conditional: it is present in the schema only when the
    connector is provisioned (URL + internal key), so an un-provisioned run pays nothing and no
    other tool is affected.
    """
    ctx.register_tool(
        name="github_api",
        toolset="github",
        schema=GITHUB_API_SCHEMA,
        handler=lambda args, **kw: handle_github_api(args, **kw),
        check_fn=check_github_requirements,
        emoji="🐙",
    )
    logger.debug("axis-github: github_api enregistré (conditionnel au provisioning connecteur)")
