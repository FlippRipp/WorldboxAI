"""The built-in v1 agent tools. Importing this package registers them
(same registration-on-import idiom as ``worldgen.steps`` and
``enrichment.passes``): read tools, build tools (steps/passes), and the
node edit tool. Adding a tool = drop a module here and import it below
(P2)."""

from wbworldgen.worldgen.agent.tools import read  # noqa: F401
from wbworldgen.worldgen.agent.tools import build  # noqa: F401
from wbworldgen.worldgen.agent.tools import edit  # noqa: F401
from wbworldgen.worldgen.agent.tools import verify  # noqa: F401
