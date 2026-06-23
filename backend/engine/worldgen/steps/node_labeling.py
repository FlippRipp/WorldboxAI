from backend.engine.worldgen.base import Step, register, USES_ENRICHMENT


@register
class NodeLabelingStep(Step):
    id = "node_labeling"
    label = "Node Labeling"
    description = "Generate short labels for map nodes using a lighter LLM, from most to least important."
    after = "map_generation"
    uses = USES_ENRICHMENT
    schema = {
        "total_nodes": {"type": "number", "label": "Nodes to Label", "default": 0},
        "labeled_count": {"type": "number", "label": "Total Labeled", "default": 0},
    }
