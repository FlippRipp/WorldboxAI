"""Default pipeline steps.

Importing this package registers every default step into the global
``STEP_REGISTRY`` (via the ``@register`` decorator on each class). To add a new
step, create a module here and decorate its ``Step`` subclass with
``@register``, then import it below.
"""

from wbworldgen.worldgen.steps.world_rules import WorldRulesStep
from wbworldgen.worldgen.steps.lore import LoreStep
from wbworldgen.worldgen.steps.layer_design import LayerDesignStep
from wbworldgen.worldgen.steps.layer_rules import LayerRulesStep
from wbworldgen.worldgen.steps.terrain_generation import TerrainGenerationStep
from wbworldgen.worldgen.steps.terrain_regions import TerrainRegionsStep
from wbworldgen.worldgen.steps.natural_landmarks import NaturalLandmarksStep
from wbworldgen.worldgen.steps.society_factions import SocietyFactionsStep
from wbworldgen.worldgen.steps.map_generation import MapGenerationStep
from wbworldgen.worldgen.steps.node_labeling import NodeLabelingStep
from wbworldgen.worldgen.steps.node_descriptions import NodeDescriptionsStep

__all__ = [
    "WorldRulesStep",
    "LoreStep",
    "LayerDesignStep",
    "LayerRulesStep",
    "TerrainGenerationStep",
    "TerrainRegionsStep",
    "NaturalLandmarksStep",
    "SocietyFactionsStep",
    "MapGenerationStep",
    "NodeLabelingStep",
    "NodeDescriptionsStep",
]
