from wbworldgen.worldgen.base import Step, register, USES_ENRICHMENT


@register
class NodeDescriptionsStep(Step):
    id = "node_descriptions"
    label = "Node Descriptions"
    description = "Generate rich flavor descriptions for labeled map nodes."
    after = "node_labeling"
    uses = USES_ENRICHMENT
    schema = {
        "total_nodes": {"type": "number", "label": "Nodes to Describe", "default": 0},
        "described_count": {"type": "number", "label": "Total Described", "default": 0},
    }
