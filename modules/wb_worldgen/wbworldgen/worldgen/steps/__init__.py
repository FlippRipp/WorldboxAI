"""Default pipeline steps.

Importing this package registers every default step into the global
``STEP_REGISTRY`` (via the ``@register`` decorator on each class). To add a new
step, create a module here and decorate its ``Step`` subclass with
``@register``, then import it below.

Deprecated (kept on disk, no longer registered): layer_design + layer_rules
(replaced by hierarchy_design — parallel maps in the world hierarchy) and
terrain_regions (see docs/systems/DEPRECATED_regions.md).
"""

from wbworldgen.worldgen.steps.world_form import WorldFormStep
from wbworldgen.worldgen.steps.world_rules import WorldRulesStep
from wbworldgen.worldgen.steps.lore import LoreStep
from wbworldgen.worldgen.steps.codex import CodexStep
from wbworldgen.worldgen.steps.hierarchy_design import HierarchyDesignStep
from wbworldgen.worldgen.steps.terrain_generation import TerrainGenerationStep
from wbworldgen.worldgen.steps.natural_landmarks import NaturalLandmarksStep
from wbworldgen.worldgen.steps.society_factions import SocietyFactionsStep
from wbworldgen.worldgen.steps.map_generation import MapGenerationStep
from wbworldgen.worldgen.steps.node_labeling import NodeLabelingStep
from wbworldgen.worldgen.steps.node_descriptions import NodeDescriptionsStep

__all__ = [
    "WorldFormStep",
    "WorldRulesStep",
    "LoreStep",
    "CodexStep",
    "HierarchyDesignStep",
    "TerrainGenerationStep",
    "NaturalLandmarksStep",
    "SocietyFactionsStep",
    "MapGenerationStep",
    "NodeLabelingStep",
    "NodeDescriptionsStep",
]
