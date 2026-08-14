"""axis-microsoft plugin — additive Microsoft Graph tool authenticated by the Axis connectors service.

Registers a single tool (`microsoft_graph`) ON TOP of the built-in toolset — it never overrides or
removes anything (narrow-waist doctrine). Gated by `check_microsoft_requirements`, so it only surfaces
when the Axis platform has provisioned the connector for the run (env injected by the Timonier). The
delegated auth (OAuth client secret + per-user refresh token) lives entirely in the Axis platform; the
sandbox holds no credential.
"""

from __future__ import annotations

import logging

from plugins.axis_microsoft.tools import (
    MICROSOFT_GRAPH_SCHEMA,
    check_microsoft_requirements,
    handle_microsoft_graph,
)

logger = logging.getLogger("axis.microsoft")


def register(ctx) -> None:
    """Register the `microsoft_graph` tool. Called once by the plugin loader when enabled.

    `check_fn` keeps the tool additive-and-conditional: present only when the connector is provisioned.
    """
    ctx.register_tool(
        name="microsoft_graph",
        toolset="microsoft",
        schema=MICROSOFT_GRAPH_SCHEMA,
        handler=lambda args, **kw: handle_microsoft_graph(args, **kw),
        check_fn=check_microsoft_requirements,
        emoji="🪟",
    )
    logger.debug("axis-microsoft: microsoft_graph enregistré (conditionnel au provisioning connecteur)")
