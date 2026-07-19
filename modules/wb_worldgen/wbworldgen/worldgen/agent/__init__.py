"""The agent toolbox (Arc C of the worldgen architecture plan).

C1's server-side substrate for the agentic builder: the fourth capability
catalog (``ToolSpec`` registry — the agent's action surface over steps,
generators and passes), the deterministic lint report (D3's cheap ground
truth), and the v1 tools themselves (``tools/``). The C2 harness drives
builds by rendering ``describe_tools()`` into the agent's system prompt and
executing its JSON actions through ``invoke_tool``.
"""

from wbworldgen.worldgen.agent.registry import (  # noqa: F401
    ToolContext,
    ToolError,
    ToolSpec,
    describe_tools,
    get_tool,
    invoke_tool,
    register_tool,
    registered_tools,
    unregister_tool,
    validate_args,
)
from wbworldgen.worldgen.agent.lints import lint_world  # noqa: F401
