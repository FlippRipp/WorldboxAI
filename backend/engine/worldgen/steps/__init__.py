"""Default pipeline steps.

Importing this package registers every default step into the global
``STEP_REGISTRY`` (via the ``@register`` decorator on each class). To add a new
step, create a module here and decorate its ``Step`` subclass with
``@register``, then import it below.
"""

from backend.engine.worldgen.steps.world_rules import WorldRulesStep
from backend.engine.worldgen.steps.lore import LoreStep
from backend.engine.worldgen.steps.layer_design import LayerDesignStep
from backend.engine.worldgen.steps.layer_rules import LayerRulesStep
from backend.engine.worldgen.steps.terrain_regions import TerrainRegionsStep
from backend.engine.worldgen.steps.natural_landmarks import NaturalLandmarksStep
from backend.engine.worldgen.steps.society_factions import SocietyFactionsStep
from backend.engine.worldgen.steps.map_generation import MapGenerationStep
from backend.engine.worldgen.steps.node_labeling import NodeLabelingStep
from backend.engine.worldgen.steps.node_descriptions import NodeDescriptionsStep

__all__ = [
    "WorldRulesStep",
    "LoreStep",
    "LayerDesignStep",
    "LayerRulesStep",
    "TerrainRegionsStep",
    "NaturalLandmarksStep",
    "SocietyFactionsStep",
    "MapGenerationStep",
    "NodeLabelingStep",
    "NodeDescriptionsStep",
]
