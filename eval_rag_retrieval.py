"""
Standalone RAG retrieval reliability eval.

Seeds the real MemoryManager (LanceDB) with a small hand-labeled corpus of
memories and world lore, embeds a set of natural-language queries with the
real Gemini embedding model, and reports Recall@K / MRR for both
`search_memories` and `search_world`.

This is NOT a pytest test: it makes real network calls to the Gemini
embedding API (the model configured as LLMService.embedding_model) and costs
a small number of API calls. Run manually:

    python eval_rag_retrieval.py

Requires GEMINI_API_KEY to be set (loaded from backend/.env).
"""
import asyncio
import os
import sys
import tempfile

from dotenv import load_dotenv

load_dotenv("backend/.env")
os.environ["LLM_DEBUG"] = ""  # keep the per-call request/response dump out of the eval report

from backend.engine.llm import LLMService
from backend.engine.memory import MemoryManager


# ---------------------------------------------------------------------------
# Labeled memory corpus
# ---------------------------------------------------------------------------
# Each entry: id, text (raw narrative), summary, entities, topics.
# Several entries deliberately share a topic/entity (combat, the same NPC)
# so the eval also exercises discrimination, not just bare retrieval.
MEMORY_CORPUS = [
    {
        "id": "mem_dragon_fight",
        "text": "The hero climbed the mountain pass and battled a red dragon guarding the old shrine, driving it off with a flaming sword.",
        "summary": "Hero fought off a red dragon at the mountain shrine.",
        "entities": ["Hero", "Dragon", "Mountain Pass", "Shrine"],
        "topics": ["combat", "exploration"],
        "importance": 8,
    },
    {
        "id": "mem_goblin_ambush",
        "text": "A pack of goblins ambushed the caravan near the river crossing, stealing two crates of supplies before fleeing into the woods.",
        "summary": "Goblins ambushed the caravan at the river crossing and stole supplies.",
        "entities": ["Goblins", "Caravan", "River Crossing"],
        "topics": ["combat", "theft"],
        "importance": 4,
    },
    {
        "id": "mem_peace_treaty",
        "text": "Representatives of the Ashfen Clan and the Highland Coalition met at the council hall and signed a treaty ending the border conflict.",
        "summary": "Ashfen Clan and Highland Coalition signed a peace treaty.",
        "entities": ["Ashfen Clan", "Highland Coalition", "Council Hall"],
        "topics": ["diplomacy", "politics"],
        "importance": 9,
    },
    {
        "id": "mem_merchant_betrayal",
        "text": "The merchant Corvin was caught smuggling stolen relics out of the city and was arrested by the city guard at the docks.",
        "summary": "Merchant Corvin was arrested for smuggling stolen relics.",
        "entities": ["Corvin", "City Guard", "Docks"],
        "topics": ["crime", "betrayal"],
        "importance": 6,
    },
    {
        "id": "mem_corvin_backstory",
        "text": "Corvin once served as a royal treasurer before he was exiled for forging trade ledgers a decade ago.",
        "summary": "Corvin was exiled years ago for forging trade ledgers as royal treasurer.",
        "entities": ["Corvin"],
        "topics": ["backstory", "crime"],
        "importance": 5,
    },
    {
        "id": "mem_haunted_lighthouse",
        "text": "Sailors refuse to approach the old lighthouse on Gray Point after dark, claiming a ghostly light leads ships onto the rocks.",
        "summary": "The Gray Point lighthouse is rumored to be haunted and lures ships onto rocks.",
        "entities": ["Lighthouse", "Gray Point", "Sailors"],
        "topics": ["mystery", "rumor"],
        "importance": 5,
    },
    {
        "id": "mem_plague_outbreak",
        "text": "A fever swept through the lower district of the capital, killing dozens before the healers isolated the source to tainted well water.",
        "summary": "A fever outbreak in the capital's lower district was traced to tainted well water.",
        "entities": ["Capital", "Lower District", "Healers"],
        "topics": ["disaster", "mystery"],
        "importance": 7,
    },
    {
        "id": "mem_arena_champion",
        "text": "Talia won her third consecutive bout in the gladiator arena, defeating a heavily armored ogre with nothing but a spear.",
        "summary": "Talia defeated an ogre in the gladiator arena, her third straight win.",
        "entities": ["Talia", "Arena", "Ogre"],
        "topics": ["combat", "sport"],
        "importance": 4,
    },
    {
        "id": "mem_lost_heirloom",
        "text": "An old woman in the market square is offering a reward for the return of her late husband's silver locket, lost during the flood.",
        "summary": "An old woman seeks her husband's silver locket, lost in a flood.",
        "entities": ["Market Square", "Silver Locket"],
        "topics": ["quest", "loss"],
        "importance": 3,
    },
    {
        "id": "mem_smugglers_tunnel",
        "text": "A hidden tunnel beneath the tannery connects to the old sewers, used by smugglers to move goods past the city gate tax.",
        "summary": "Smugglers use a hidden tunnel under the tannery to bypass city gate taxes.",
        "entities": ["Tannery", "Sewers", "Smugglers"],
        "topics": ["crime", "secret"],
        "importance": 5,
    },
    {
        "id": "mem_festival_announcement",
        "text": "The town crier announced the upcoming harvest festival, with games, a feast, and a tournament to be held in the central plaza.",
        "summary": "A harvest festival with games and a tournament was announced for the central plaza.",
        "entities": ["Town Crier", "Harvest Festival", "Central Plaza"],
        "topics": ["event", "celebration"],
        "importance": 3,
    },
    {
        "id": "mem_wolf_pack_sighting",
        "text": "Shepherds near the northern hills reported an unusually large wolf pack stalking their flocks at dusk for three nights running.",
        "summary": "A large wolf pack has been stalking flocks in the northern hills.",
        "entities": ["Shepherds", "Northern Hills", "Wolf Pack"],
        "topics": ["danger", "rumor"],
        "importance": 4,
    },
]

# (query, list of acceptable expected memory ids)
MEMORY_QUERIES = [
    ("What happened with the dragon?", ["mem_dragon_fight"]),
    ("Tell me about the goblin attack on the supply wagons.", ["mem_goblin_ambush"]),
    ("What's the status of the war between the clans?", ["mem_peace_treaty"]),
    ("Why was Corvin arrested?", ["mem_merchant_betrayal"]),
    ("What is Corvin's history before he became a merchant?", ["mem_corvin_backstory"]),
    ("Is the lighthouse on Gray Point really haunted?", ["mem_haunted_lighthouse"]),
    ("What caused the sickness in the capital?", ["mem_plague_outbreak"]),
    ("How is Talia doing in the arena?", ["mem_arena_champion"]),
    ("Has anyone found the old woman's lost locket?", ["mem_lost_heirloom"]),
    ("How are smugglers avoiding the gate tax?", ["mem_smugglers_tunnel"]),
    ("When is the harvest festival happening?", ["mem_festival_announcement"]),
    ("Are there dangerous animals near the northern hills?", ["mem_wolf_pack_sighting"]),
]


# ---------------------------------------------------------------------------
# Labeled world corpus (built via the real _build_world_entries pipeline)
# ---------------------------------------------------------------------------
WORLD_DATA = {
    "lore": {
        "premise": "A fractured continent where ancient magic is slowly fading and city-states compete for the last sources of power.",
        "central_conflict": "The Ember Pact and the Verdant Accord are locked in a cold war over control of the dwindling leyline network.",
        "creation_myth": "The world was said to be sung into being by twin sky-serpents, whose final breath became the first leyline.",
        "historical_eras": [
            {"name": "The Sundering", "summary": "A cataclysm split the continent into three floating shelves of land."},
        ],
    },
    "regions": {
        "regions": [
            {
                "name": "Ember Reach",
                "terrain": "volcanic badlands",
                "climate": "hot and dry",
                "landmarks": ["The Cinderspire"],
                "named_locations": [
                    {"name": "The Cinderspire", "category": "landmark", "description": "A black obsidian tower that channels volcanic leyline energy for the Ember Pact."},
                ],
                "factions": ["Ember Pact"],
                "faction_details": [
                    {"name": "Ember Pact", "type": "militant theocracy", "description": "Worships the sky-serpents and controls the volcanic leylines.", "settlements": ["Cinder Hold"]},
                ],
            },
            {
                "name": "Verdant Vale",
                "terrain": "dense rainforest",
                "climate": "humid and temperate",
                "landmarks": ["The Sunken Library"],
                "named_locations": [
                    {"name": "The Sunken Library", "category": "landmark", "description": "A flooded archive holding pre-Sundering texts, guarded by the Verdant Accord."},
                ],
                "factions": ["Verdant Accord"],
                "faction_details": [
                    {"name": "Verdant Accord", "type": "scholarly republic", "description": "A council of archivists and druids seeking to restore the old magic safely.", "settlements": ["Greenhall"]},
                ],
            },
        ],
    },
    "map": {
        "nodes": [
            {"id": "node_cinder_hold", "name": "Cinder Hold", "type": "settlement", "description": "The fortified capital of the Ember Pact, built into the side of an active volcano."},
            {"id": "node_greenhall", "name": "Greenhall", "type": "settlement", "description": "The capital of the Verdant Accord, a city grown from living trees."},
        ],
    },
}

# (query, list of acceptable expected world source_ids)
WORLD_QUERIES = [
    ("What is the central conflict of this world?", ["central_conflict"]),
    ("How was the world created?", ["creation_myth"]),
    ("Tell me about the volcanic region.", ["Ember Reach"]),
    ("What does the Cinderspire do?", ["The Cinderspire"]),
    ("Who are the Ember Pact?", ["Ember Pact"]),
    ("What's inside the Sunken Library?", ["The Sunken Library"]),
    ("Who leads the Verdant Accord?", ["Verdant Accord"]),
    ("Describe the capital of the Ember Pact.", ["node_cinder_hold"]),
    ("Describe Greenhall.", ["node_greenhall"]),
    ("What happened during the Sundering?", ["The Sundering"]),
]

PRODUCTION_MEMORY_LIMIT = 3
PRODUCTION_WORLD_LIMIT = 2
LARGE_LIMIT = 10
RECALL_THRESHOLD = 0.7


def reciprocal_rank(retrieved_ids, expected_ids):
    for rank, rid in enumerate(retrieved_ids, start=1):
        if rid in expected_ids:
            return rank
    return None


async def run_memory_eval(llm: LLMService, db_path: str):
    manager = MemoryManager(db_path, embedding_dim=await probe_dim(llm))
    id_map = {}
    for i, entry in enumerate(MEMORY_CORPUS):
        vec = await llm.get_embedding(entry["text"])
        memory_id = manager.add_memory(
            vec, entry["text"], turn=1, importance=entry["importance"],
            summary=entry["summary"], entities=entry["entities"], topics=entry["topics"],
        )
        id_map[memory_id] = entry["id"]

    rows = []
    for query, expected in MEMORY_QUERIES:
        qvec = await llm.get_embedding(query)
        large_results = manager.search_memories(qvec, current_turn=1, limit=LARGE_LIMIT)
        retrieved_ids = [id_map.get(r["id"], r["id"]) for r in large_results]
        rank = reciprocal_rank(retrieved_ids, expected)
        hit_at_k = any(rid in expected for rid in retrieved_ids[:PRODUCTION_MEMORY_LIMIT])
        rows.append({
            "query": query,
            "expected": expected,
            "retrieved": retrieved_ids[:PRODUCTION_MEMORY_LIMIT],
            "rank": rank,
            "hit_at_k": hit_at_k,
        })
    return rows


async def run_world_eval(llm: LLMService, db_path: str):
    manager = MemoryManager(db_path, embedding_dim=await probe_dim(llm))
    manager.init_world_index(db_path + "_world")
    await manager.embed_world(WORLD_DATA, llm)

    rows = []
    for query, expected in WORLD_QUERIES:
        qvec = await llm.get_embedding(query)
        large_results = manager.search_world(qvec, limit=LARGE_LIMIT)
        retrieved_ids = [r["source_id"] for r in large_results]
        rank = reciprocal_rank(retrieved_ids, expected)
        hit_at_k = any(rid in expected for rid in retrieved_ids[:PRODUCTION_WORLD_LIMIT])
        rows.append({
            "query": query,
            "expected": expected,
            "retrieved": retrieved_ids[:PRODUCTION_WORLD_LIMIT],
            "rank": rank,
            "hit_at_k": hit_at_k,
        })
    return rows


_dim_cache = None


async def probe_dim(llm: LLMService) -> int:
    global _dim_cache
    if _dim_cache is None:
        vec = await llm.get_embedding("dimension probe")
        _dim_cache = len(vec)
    return _dim_cache


def print_report(title: str, rows: list[dict]):
    print(f"\n=== {title} ===")
    hits = 0
    reciprocal_ranks = []
    for row in rows:
        status = "HIT " if row["hit_at_k"] else "MISS"
        rank_str = str(row["rank"]) if row["rank"] is not None else "not found"
        print(f"[{status}] rank={rank_str:>9}  query={row['query']!r}")
        print(f"        expected={row['expected']}  retrieved={row['retrieved']}")
        if row["hit_at_k"]:
            hits += 1
        reciprocal_ranks.append(1 / row["rank"] if row["rank"] else 0.0)

    n = len(rows)
    recall_at_k = hits / n if n else 0.0
    mrr = sum(reciprocal_ranks) / n if n else 0.0
    print(f"\n{title} summary: Recall@K={recall_at_k:.2f} ({hits}/{n})  MRR={mrr:.2f}")
    return recall_at_k


async def main():
    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not set (checked backend/.env). Aborting.")
        sys.exit(1)

    llm = LLMService(mode="live")

    with tempfile.TemporaryDirectory() as tmpdir:
        memory_rows = await run_memory_eval(llm, os.path.join(tmpdir, "memory"))
        world_rows = await run_world_eval(llm, os.path.join(tmpdir, "world"))

    memory_recall = print_report("Memory Retrieval", memory_rows)
    world_recall = print_report("World Knowledge Retrieval", world_rows)

    print(f"\n{'='*60}")
    overall_ok = memory_recall >= RECALL_THRESHOLD and world_recall >= RECALL_THRESHOLD
    print(f"Threshold: Recall@K >= {RECALL_THRESHOLD:.2f}  ->  {'PASS' if overall_ok else 'FAIL'}")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
