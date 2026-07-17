"""Hardcoded mock fixture data for dev/demo generation without an LLM.

Each function takes ``(prompt, note="")`` and returns the same data the legacy
``WorldBuilder._mock_*`` methods produced. ``MOCK_GENERATORS`` maps a step id
to its fixture function so generation can be dispatched by id.
"""


def mock_rules(prompt: str, note: str = "") -> dict:
    return {
        "genre": "dark fantasy",
        "tone": "grim and mysterious",
        "magic_level": "rare",
        "tech_era": "iron age",
        "lethality": 7,
        "custom_rules": [
            "Magic always has a cost",
            "The dead do not rest easily",
            "Technology cannot exceed basic machinery",
        ],
    }


def mock_lore(prompt: str, note: str = "") -> dict:
    return {
        "world_name": "Mycelium",
        "premise": "Beneath the rotting surface of a forgotten world, sentient fungal networks have built civilizations in the dark. Spores carry memories, and ancient roots remember what the surface has long forgotten.",
        "creation_myth": "The Great Spore fell from the void and took root in the corpse of the old world. From its mycelium sprang the first thinking minds.",
        "historical_eras": [
            {"name": "The Seeding", "duration": "0-1000", "summary": "The Great Spore spreads, first fungal minds awaken."},
            {"name": "The Root Wars", "duration": "1000-2000", "summary": "Competing mycelial networks wage war for territory and nutrients."},
            {"name": "The Surface Scourge", "duration": "2000-2500", "summary": "Something from above forced the fungi deeper underground."},
            {"name": "Current Era", "duration": "2500-present", "summary": "Fragile peace between the great hyphal networks. Explorers venture upward again."},
        ],
        "central_conflict": "The surface is calling — but what drove the fungi underground still lurks above.",
    }


def mock_terrain_regions(prompt: str, note: str = "") -> dict:
    has_layers = bool(note and "layer" in note.lower())
    if has_layers:
        return {
            "regions": [
                {
                    "layer_id": "overworld",
                    "name": "The Ash Wastes",
                    "terrain": "barren surface deadlands, petrified forests, toxic soil",
                    "climate": "cold, harsh winds, toxic spore clouds",
                    "description": "The ruined surface of the world, scorched by an ancient cataclysm. Few living things survive in the open air.",
                },
                {
                    "layer_id": "overworld",
                    "name": "The Verdant Coast",
                    "terrain": "coastal cliffs, sheltered coves, salt marshes",
                    "climate": "temperate, frequent sea storms, salty air",
                    "description": "A narrow strip of habitable coastline where hardy communities cling to life between the ash-choked interior and the stormy sea.",
                },
                {
                    "layer_id": "underground",
                    "name": "The Spirewood",
                    "terrain": "towering fungal forests with caps that scrape the cavern ceiling",
                    "climate": "warm and humid, perpetual spore-fall",
                    "description": "A vast underground biome of giant fungi, glowing spores, and mycelial networks that hum with ancient memories.",
                },
                {
                    "layer_id": "underground",
                    "name": "The Deep Mycelium",
                    "terrain": "dense root networks stretching for miles, bioluminescent veins",
                    "climate": "cool and damp, no natural light",
                    "description": "The heart of the fungal civilization, where ancient root systems form the infrastructure of subterranean society.",
                },
            ],
        }
    return {
        "regions": [
            {
                "layer_id": "",
                "name": "The Spirewood",
                "terrain": "towering fungal forests with caps that scrape the cavern ceiling",
                "climate": "warm and humid, perpetual spore-fall",
                "description": "A vast underground biome of giant fungi, glowing spores, and mycelial networks that hum with ancient memories.",
            },
            {
                "layer_id": "",
                "name": "The Deep Mycelium",
                "terrain": "dense root networks stretching for miles, bioluminescent veins",
                "climate": "cool and damp, no natural light",
                "description": "The heart of the fungal civilization, where ancient root systems form the infrastructure of subterranean society.",
            },
            {
                "layer_id": "",
                "name": "The Ash Wastes",
                "terrain": "barren surface deadlands, petrified forests, toxic soil",
                "climate": "cold, harsh winds, toxic spore clouds",
                "description": "The ruined surface of the world, scorched by an ancient cataclysm. Few living things survive in the open air.",
            },
        ],
    }


def mock_natural_landmarks(prompt: str, note: str = "") -> dict:
    has_layers = bool(note and "layer" in note.lower())
    if has_layers:
        return {
            "landmarks": [
                {"layer_id": "overworld", "region": "The Ash Wastes", "name": "The Great Husk", "type": "petrified_forest", "description": "A sprawling forest turned to stone, its branches reaching skyward like skeletal fingers."},
                {"layer_id": "overworld", "region": "The Ash Wastes", "name": "Bone Fields", "type": "boneyard", "description": "Miles of ancient skeletons half-buried in ash, remnants of the cataclysm."},
                {"layer_id": "overworld", "region": "The Verdant Coast", "name": "Stormglass Cliffs", "type": "coastal_formation", "description": "Towering cliffs striated with crystalline veins that glow during thunderstorms."},
                {"layer_id": "underground", "region": "The Spirewood", "name": "The Mother Cap", "type": "giant_fungus", "description": "The largest known fungal cap, its gills spanning half a mile and dripping with luminous nectar."},
                {"layer_id": "underground", "region": "The Spirewood", "name": "Glowworm Grotto", "type": "cavern", "description": "A vast cave illuminated by millions of bioluminescent worms that paint the walls in shifting patterns."},
                {"layer_id": "underground", "region": "The Deep Mycelium", "name": "The Heartroot", "type": "mycelial_node", "description": "A massive pulsing root nexus believed to be the origin of fungal sentience."},
            ],
        }
    return {
        "landmarks": [
            {"layer_id": "", "region": "The Spirewood", "name": "The Mother Cap", "type": "giant_fungus", "description": "The largest known fungal cap, its gills spanning half a mile and dripping with luminous nectar."},
            {"layer_id": "", "region": "The Spirewood", "name": "Sporefall Basin", "type": "basin", "description": "A depression where spores collect in drifts like snow, creating a hallucinogenic mist."},
            {"layer_id": "", "region": "The Spirewood", "name": "Glowworm Grotto", "type": "cavern", "description": "A vast cave illuminated by millions of bioluminescent worms that paint the walls in shifting patterns."},
            {"layer_id": "", "region": "The Deep Mycelium", "name": "The Heartroot", "type": "mycelial_node", "description": "A massive pulsing root nexus believed to be the origin of fungal sentience."},
            {"layer_id": "", "region": "The Deep Mycelium", "name": "Memory Caverns", "type": "cavern", "description": "A network of caves where the mycelium preserves ancestral memories in crystalline formations."},
            {"layer_id": "", "region": "The Ash Wastes", "name": "The Great Husk", "type": "petrified_forest", "description": "A sprawling forest turned to stone, its branches reaching skyward like skeletal fingers."},
        ],
    }


def mock_society_factions(prompt: str, note: str = "") -> dict:
    has_layers = bool(note and "layer" in note.lower())
    if has_layers:
        return {
            "factions": [
                {"layer_id": "overworld", "region": "The Ash Wastes", "name": "Ash Nomads", "type": "tribe", "description": "Survivors who traverse the wastes in caravans, collecting relics from the old world.", "settlements": ["Dusthaven"], "significant_landmarks": ["The Rust Citadel"]},
                {"layer_id": "overworld", "region": "The Ash Wastes", "name": "Surface Scouts", "type": "guild", "description": "Daring explorers who map the surface and search for resources, funded by underground factions.", "settlements": ["Outpost Zenith"], "significant_landmarks": ["Signal Tower Alpha"]},
                {"layer_id": "overworld", "region": "The Verdant Coast", "name": "Saltwardens", "type": "order", "description": "A militant order that guards the coastline against deep-sea horrors and surface raiders.", "settlements": ["Saltspire Keep"], "significant_landmarks": ["The Tidewall", "Stormwatch Lighthouse"]},
                {"layer_id": "underground", "region": "The Spirewood", "name": "The Cap Wardens", "type": "order", "description": "Guardians of the fungal forests who maintain the delicate ecological balance.", "settlements": ["Sporehold"], "significant_landmarks": ["The Mycelial Throne"]},
                {"layer_id": "underground", "region": "The Deep Mycelium", "name": "Rootwardens", "type": "order", "description": "Theocratic order that interprets the memories stored in the mycelial roots.", "settlements": ["Root-Anchor"], "significant_landmarks": ["The Memory Throne", "The Sealed Gate"]},
                {"layer_id": "underground", "region": "The Deep Mycelium", "name": "The Unremembered", "type": "cult", "description": "A secretive faction that seeks to erase certain memories from the mycelium.", "settlements": [], "significant_landmarks": []},
            ],
        }
    return {
        "factions": [
            {"layer_id": "", "region": "The Spirewood", "name": "The Cap Wardens", "type": "order", "description": "Guardians of the fungal forests who maintain the delicate ecological balance.", "settlements": ["Sporehold"], "significant_landmarks": ["The Mycelial Throne"]},
            {"layer_id": "", "region": "The Spirewood", "name": "Spore Merchants Guild", "type": "guild", "description": "Traders who collect and distribute rare spore varieties across the underground.", "settlements": ["Sporedock"], "significant_landmarks": []},
            {"layer_id": "", "region": "The Deep Mycelium", "name": "Rootwardens", "type": "order", "description": "Theocratic order that interprets the memories stored in the mycelial roots.", "settlements": ["Root-Anchor"], "significant_landmarks": ["The Memory Throne", "The Sealed Gate"]},
            {"layer_id": "", "region": "The Deep Mycelium", "name": "The Unremembered", "type": "cult", "description": "A secretive faction that seeks to erase certain memories from the mycelium.", "settlements": [], "significant_landmarks": []},
            {"layer_id": "", "region": "The Ash Wastes", "name": "Surface Scouts", "type": "guild", "description": "Daring explorers who map the surface and search for resources, funded by underground factions.", "settlements": ["Outpost Zenith"], "significant_landmarks": []},
            {"layer_id": "", "region": "The Ash Wastes", "name": "Ash Nomads", "type": "tribe", "description": "Survivors who traverse the wastes in caravans, collecting relics from the old world.", "settlements": ["Dusthaven"], "significant_landmarks": ["The Rust Citadel"]},
        ],
    }


def mock_layer_design(prompt: str, note: str = "") -> dict:
    return {
        "has_multiple_layers": True,
        "layers": [
            {
                "layer_id": "overworld",
                "name": "The Sunlit Surface",
                "layer_type": "surface",
                "description": "The surface continent of Aethra, with its kingdoms, forests, seas, and the Ash Wastes. A land of harsh sunlight, scattered human enclaves, and petrified ruins of the old world.",
                "index": 0,
            },
            {
                "layer_id": "underground",
                "name": "The Deep Mycelium",
                "layer_type": "underground",
                "description": "A vast network of fungal caverns beneath the surface, home to sentient mycelial civilizations. Bioluminescent forests, memory caverns, and the heart of the fungal hivemind.",
                "index": 1,
            },
        ],
        "connections": [
            {
                "from_layer": "overworld",
                "to_layer": "underground",
                "connection_type": "dungeon_entrance",
                "description": "Ancient sinkholes and forgotten temple entrances dot the surface, leading down into the fungal depths below.",
                "count_hint": 3,
            },
        ],
    }


def mock_layer_rules(prompt: str, note: str = "") -> dict:
    return {
        "layer_rules": [
            {
                "layer_id": "overworld",
                "name": "The Sunlit Surface",
                "rules": [
                    "Solar storms blast the surface every third day — travel only safe during calm windows",
                    "Surface settlements are built into cliff faces or underground bunkers to shield against the storms",
                ],
            },
            {
                "layer_id": "underground",
                "name": "The Deep Mycelium",
                "rules": [
                    "Fungal spores carry ancestral memories — breathing them too long causes identity loss",
                    "Natural light is lethal to pure fungal beings; even a lantern can scar them",
                ],
            },
        ],
        "world_rules": [
            "The dead do not rest easily and will rise if not properly cremated",
            "Magic always demands an equivalent sacrifice — the greater the effect, the heavier the cost",
            "Oaths spoken under moonlight become magically binding and cannot be broken without consequence",
        ],
    }


def mock_world_form(prompt: str, note: str = "") -> dict:
    # Terrain style + no skips: seeded/offline worlds behave exactly like
    # worlds generated before the world_form step existed. City-flavored
    # prompts pick the street-network map so offline runs exercise it.
    text = (prompt or "").lower()
    if any(w in text for w in ("city", "metropolis", "urban", "town")):
        return {
            "world_kind": "A single modern city of districts and streets (mock).",
            "map_style": "city",
            "skip_steps": [],
            "step_directives": [
                {"step_id": "lore", "directive": "Name the city and sketch its history."},
                {"step_id": "society_factions", "directive": "Author the city's powers."},
            ],
        }
    return {
        "world_kind": "A dark fantasy overworld of ancient powers (mock).",
        "map_style": "terrain",
        "skip_steps": [],
        "step_directives": [
            {"step_id": "lore", "directive": "Name the world and give it a mythic deep history."},
            {"step_id": "society_factions", "directive": "Author the great powers of the world."},
        ],
    }


#: Maps step id -> fixture function. Used by the mock generator.
def mock_hierarchy_design(prompt: str, note: str = "") -> dict:
    return {
        "notes": "A single overworld map; key locations open into their own "
                 "interior maps during play.",
        "parallel_maps": [],
        "pregenerate": [],
    }


MOCK_GENERATORS = {
    "world_form": mock_world_form,
    "world_rules": mock_rules,
    "lore": mock_lore,
    "hierarchy_design": mock_hierarchy_design,
    "natural_landmarks": mock_natural_landmarks,
    "society_factions": mock_society_factions,
    # Deprecated steps (not registered by default; kept for legacy worlds/tests).
    "layer_design": mock_layer_design,
    "layer_rules": mock_layer_rules,
    "terrain_regions": mock_terrain_regions,
}
