"""Child-map and site generation: growing new maps under existing nodes.

``maps_expand`` descends the designed hierarchy (child maps for expandable
nodes, authored/procedural roots); ``sites`` grows interior site maps for
individual locations. Both are *generation*, not enrichment — they live here
so ``enrichment/`` can stay the node-pass engine. Module-level helpers
(``map_world_entries``, ``allowed_child_levels``, each module's
``is_expandable``) are imported from their module directly; the two
``is_expandable`` functions answer different questions (map-node vs site),
so this package deliberately re-exports neither.
"""

from wbworldgen.worldgen.expansion.maps_expand import MapExpansionEngine
from wbworldgen.worldgen.expansion.sites import SiteExpansionEngine, site_world_entries

__all__ = [
    "MapExpansionEngine",
    "SiteExpansionEngine",
    "site_world_entries",
]
