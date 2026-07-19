"""Built-in enrichment passes. Importing this package registers them —
registration order (label, describe, review) is the summary-key order runs
report."""

from wbworldgen.worldgen.enrichment.passes import label  # noqa: F401
from wbworldgen.worldgen.enrichment.passes import describe  # noqa: F401
from wbworldgen.worldgen.enrichment.passes import review  # noqa: F401
