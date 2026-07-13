"""Image Generation -- illustrates the story with Novita AI text-to-image.

Every N storyteller generations (or on demand via /image) the latest narration
is condensed into an image prompt by the smartest LLM slot, then submitted to
Novita's async txt2img API. The whole pipeline runs as a fire-and-forget
background task so the player keeps playing while the image renders; the
chat-feed footer widget polls the module's index and shows the image under the
turn it illustrates. Novita hosts thousands of community checkpoints, so the
model is picked via a searchable dropdown backed by the /models proxy below.

Config is global (one Novita key for all stories), owned by this module and
edited in the Image Studio main-menu screen -- not in per-save settings.
"""
import asyncio
import csv
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

MODULE_ID = "wb_image_gen"

NOVITA_BASE = "https://api.novita.ai"
CIVITAI_BASE = "https://civitai.com/api/v1"

# Built-in first-party model routed to /v3/async/flux-2-dev (not in /v3/model).
# Its LoRAs are passed as download URLs, so any Civitai Flux LoRA works directly.
FLUX2_MODEL_NAME = "flux-2-dev"
FLUX2_SIZE_MIN, FLUX2_SIZE_MAX = 256, 1536

CIVITAI_SORTS = ["Most Downloaded", "Newest", "Highest Rated"]
CIVITAI_LORA_TYPES = ["LORA", "LoCon", "DoRA"]
# Civitai's nsfw param: false = SFW only, true = mixed. "only" post-filters.
CIVITAI_NSFW_MODES = ["off", "include", "only"]
# Queried searches (Meilisearch, relevance-ordered) are deep-fetched this many
# 100-item pages per request so the proxy-side sort covers a wide net.
CIVITAI_SEARCH_PAGES = 3
# Civitai's category tags. The API's `tag=` filter does not compose with
# `query=` (empty result), so queried searches post-filter on model tags.
CIVITAI_CATEGORIES = [
    "character", "style", "concept", "clothing", "poses", "action",
    "background", "celebrity", "animal", "objects", "vehicle", "buildings",
    "assets", "tool",
]
CIVITAI_BASE_MODELS = [
    "SD 1.5", "SDXL 1.0", "Pony", "Illustrious", "NoobAI", "Flux.1 D", "Flux.2 D",
]

HF_API_BASE = "https://huggingface.co/api"
HF_PAGE_BASE = "https://huggingface.co"
HF_SORTS = ["Most Downloaded", "Most Liked", "Recently Updated"]
HF_SORT_PARAMS = {
    "Most Downloaded": "downloads",
    "Most Liked": "likes",
    "Recently Updated": "lastModified",
}
# Civitai-style family name -> the canonical base_model tag used to filter the
# Hub search. The reverse mapping (_hf_base_model_name) is substring-based and
# forgiving, since repos tag many spellings of the same base.
HF_BASE_MODELS = {
    "SD 1.5": "base_model:runwayml/stable-diffusion-v1-5",
    "SDXL 1.0": "base_model:stabilityai/stable-diffusion-xl-base-1.0",
    "Pony": "base_model:AstraliteHeart/pony-diffusion-v6",
    "Illustrious": "base_model:OnomaAIResearch/Illustrious-xl-early-release-v0",
    "Flux.1 D": "base_model:black-forest-labs/FLUX.1-dev",
    "Flux.2 D": "base_model:black-forest-labs/FLUX.2-dev",
}
HF_NSFW_TAG = "not-for-all-audiences"
# The Hub's listing endpoint has no file hashes; each result needs one
# /api/models/{repo}?blobs=true call to learn its safetensors SHA256s.
HF_DETAIL_CONCURRENCY = 8
HF_DETAIL_CACHE_TTL_S = 3600

LORA_LIBRARY_MAX = 200
SD_LORAS_MAX = 5        # per txt2img request
FLUX_LORAS_MAX = 3      # flux-2-dev accepts up to 3 URL loras
NOVITA_UPLOAD_SLOTS = 5  # Novita's console cap on custom LoRA uploads

# Novita's /v3/model truncates hash_sha256 to the first 10 hex chars, its
# `name` holds the Civitai VERSION name, and filter.query cannot find Civitai
# ids -- so availability matching syncs the whole mirrored LoRA catalog
# (~2.5k entries, ~26 pages) into a hash-prefix index instead of querying.
NOVITA_HASH_PREFIX_LEN = 10
NOVITA_LORA_INDEX_TTL_S = 6 * 3600
NOVITA_LORA_INDEX_MAX_PAGES = 100

SAMPLERS = [
    "DPM++ 2M Karras",
    "DPM++ SDE Karras",
    "DPM++ 2S a Karras",
    "Euler a",
    "Euler",
    "DDIM",
    "UniPC",
    "LMS",
]
KEY_MASK_PREFIX = "****"

# Novita rejects prompts over 1024 characters.
MAX_PROMPT_CHARS = 1024

# Novita/SD tooling accepts weights well outside 0..1 (negative inverts the
# adapter); sliders, API patches, and LLM-picked weights all clamp to this.
LORA_WEIGHT_MIN = -10.0
LORA_WEIGHT_MAX = 10.0
LORA_DEFAULT_WEIGHT = 0.7
# What the per-image LLM pass decides for a LoRA: nothing, whether it
# applies (condition text), its weight (instructions text), or both.
LORA_LLM_MODES = ("off", "gate", "weight", "both")

# Profiles: named per-model setups switched in Image Studio. A profile owns
# everything checkpoint-specific (PROFILE_FIELDS) plus each LoRA's usage
# state (LORA_STATE_FIELDS); the LoRA catalog itself and account/behavior
# settings (GLOBAL_FIELDS) are shared by all profiles. The three tuples
# together cover every key of _default_config() except lora_library.
PROFILE_FIELDS = (
    "model_name", "model_base", "width", "height", "image_num", "steps",
    "guidance_scale", "sampler_name", "negative_prompt", "style_suffix",
    "pony_quality_tags", "booru_subject_mode", "booru_break_separator",
    "tag_usage_filter", "tag_usage_min_count", "prompt_template",
    "prompt_template_tags", "prompt_style_mode",
)
GLOBAL_FIELDS = (
    "enabled", "api_key", "civitai_api_key", "hf_api_key", "interval",
    "step_retries", "prompt_model_preference", "character_reference_enabled",
    "player_in_images", "chat_image_conceal", "civitai_nsfw",
)
LORA_STATE_FIELDS = ("active", "strength", "llm_mode", "condition")
PROFILES_MAX = 20
PROFILE_NAME_MAX = 60

# Character appearances come from the optional wb_character_tracker /
# wb_npc_system modules. The roster is deliberately uncapped: every present
# character reaches the prompt writer and the LoRA gate in full (see
# CLAUDE.md -- no token caps on LLM input context).
PLAYER_IN_IMAGES_MODES = ("show", "pov")
# How finished images appear in chat before the user clicks to reveal them.
CHAT_IMAGE_CONCEAL_MODES = ("off", "blur", "blackout")
# Tag models: how many characters a prompt may depict. "auto" picks single or
# multi per scene from how many tracked characters are in frame.
BOORU_SUBJECT_MODES = ("single", "multi", "auto")

# How the prompt writer phrases prompts: danbooru tag lists ("tags"),
# descriptive natural language ("natural"), or "auto" — detect from the
# checkpoint's base model / name (BOORU_TAG_MODEL_MARKERS).
PROMPT_STYLE_MODES = ("auto", "tags", "natural")

# Tag usage filter: drop LLM-produced tags that are too rare on the booru
# sites to mean anything to a tag-trained checkpoint. "soft" drops only tags
# the bundled dictionaries know but whose post count is below the threshold;
# "hard" also drops tags no dictionary has heard of (typically hallucinated).
# Trigger words, score_* tags, and BREAK always survive.
TAG_USAGE_FILTER_MODES = ("off", "soft", "hard")
DEFAULT_TAG_USAGE_MIN_COUNT = 100
# Bundled snapshots of the a1111-tagcomplete dictionaries (danbooru for anime
# tags, e621 for furry tags), merged at load time by taking each tag's
# highest count on any site. Shared format per row, no header:
# tag_name,category,post_count,"alias1,alias2"
TAG_DICT_FILES = (Path(__file__).resolve().parent / "data" / "danbooru.csv",
                  Path(__file__).resolve().parent / "data" / "e621.csv")

LORA_CONDITION_PROMPT = """You control style adapters (LoRAs) for an AI image generator. Each numbered adapter below is labeled with how you control it:
- [GATED, weight W]: decide whether its condition applies to the scene being illustrated. Include it only when the condition applies, echoing its listed weight W unchanged. Omit it otherwise.
- [ALWAYS APPLIES, pick the weight]: always include it. Choose how strongly it applies from its instructions and the scene, starting from the listed default.
- [GATED, pick the weight if it applies]: first decide whether its condition applies; omit it if not. When it applies, include it and also pick its weight from the condition text and the scene.

Be literal: a condition applies only when the scene actually shows or strongly implies it. Weights are numbers from -10 to 10 (typical range 0 to 1.5; 0 disables, negative inverts the style). When character sheets are listed, use them to recognize characters the scene mentions indirectly (by pronoun, epithet, or description).

SCENE:
{narration}{characters}

ADAPTERS:
{conditions}

Output ONLY a JSON object mapping the number of each adapter you include to its weight, e.g. {"1": 0.7, "3": 1.2}. Output {} if none apply."""

POLL_INTERVAL_S = 2.0
POLL_MAX_ITERATIONS = 240          # ~8 minutes
POLL_MAX_TRANSIENT_FAILURES = 5
SUBMIT_RETRIES = 2
# Pipeline-level retries: how many EXTRA times each step (prompt writing;
# submit+poll+download as one unit, since a failed task cannot be re-polled
# and result URLs expire) is re-run after a retryable failure.
STEP_RETRIES_DEFAULT = 1
STEP_RETRIES_MAX = 5
STEP_RETRY_BASE_DELAY_S = 2.0
# How many images one generation may render. Each image is its own Novita
# task, submitted and polled concurrently, so a batch takes roughly as long
# as a single image (but costs one generation per image).
IMAGE_NUM_MAX = 4

# Each image in a batch gets its own prompt-writer call, so the images differ
# in content, not just seed. Identical writer input tends to converge on the
# same obvious shot, so every slot past the first also carries one of these
# nudges (slot 0 stays the canonical take).
PROMPT_VARIATION_HINTS = (
    "Favor a different camera angle or distance than the obvious choice: a "
    "close-up, a wide establishing shot, from above, or from behind.",
    "Favor a different beat of the scene than the obvious choice: the moment "
    "just before or just after the peak of the action.",
    "Favor a different focus than the obvious choice: another character "
    "present, a telling detail, or the setting itself.",
)


def _variation_hint(slot: int, total: int) -> str:
    if total <= 1 or slot == 0:
        return ""
    return PROMPT_VARIATION_HINTS[(slot - 1) % len(PROMPT_VARIATION_HINTS)]


INDEX_MAX_RECORDS = 500

DEFAULT_PROMPT_TEMPLATE = """You write prompts for an AI image generator. Turn the scene below into ONE vivid image-generation prompt.

Rules:
- Describe a single striking moment from the LATEST SCENE: subjects, action, setting, lighting, mood, camera framing.
- Concrete visual language only. No story summary, no character inner thoughts, no proper-noun lore the image model cannot know -- describe what things LOOK like instead.
- Output ONLY the prompt text, no quotes, no preamble, under 150 words.

EARLIER CONTEXT (for continuity only):
{history}

LATEST SCENE (illustrate this):
{narration}"""

DEFAULT_PROMPT_TEMPLATE_TAGS = """You write prompts for an AI image generator that expects BOORU-STYLE TAGS (danbooru and e621 conventions). Turn the scene below into ONE comma-separated tag list depicting a single striking moment from the latest scene.

Rules:
- Output comma-separated booru tags, most important first: subject count (1girl, 1boy, 2girls, no humans...), then appearance (hair, eyes, clothing, species), action/pose, expression, setting, lighting, mood, composition (close-up, from above, wide shot...).
- Lowercase tag conventions: danbooru tags for human and anime-style subjects, e621 tags for anthro, feral, or creature subjects (anthro, feral, the species, fur and scale colors...). Mixing both vocabularies in one list is fine -- use whichever site's tag names the visual best.
- Concrete visual tags only -- no story summary, no proper-noun lore the image model cannot know; describe what things LOOK like instead.
- Output ONLY the tag list, no quotes, no preamble, 20-40 tags.

EARLIER CONTEXT (for continuity only):
{history}

LATEST SCENE (illustrate this):
{narration}"""

# Earlier defaults of the tags template. A stored config still carrying one of
# these verbatim was never customized, so it upgrades to the current default
# on load; edited templates are left alone.
LEGACY_PROMPT_TEMPLATES_TAGS = ("""You write prompts for an AI image generator that expects DANBOORU-STYLE TAGS. Turn the scene below into ONE comma-separated tag list depicting a single striking moment from the latest scene.

Rules:
- Output comma-separated booru tags, most important first: subject count (1girl, 1boy, 2girls, no humans...), then appearance (hair, eyes, clothing, species), action/pose, expression, setting, lighting, mood, composition (close-up, from above, wide shot...).
- Lowercase danbooru conventions. Concrete visual tags only -- no story summary, no proper-noun lore the image model cannot know; describe what things LOOK like instead.
- Output ONLY the tag list, no quotes, no preamble, 20-40 tags.

EARLIER CONTEXT (for continuity only):
{history}

LATEST SCENE (illustrate this):
{narration}""",)

DEFAULT_PONY_QUALITY_TAGS = "score_9, score_8_up, score_7_up"

# Tag-trained checkpoints blend features badly when asked for several distinct
# characters, so booru_subject_mode "single" narrows the prompt to one.
BOORU_SINGLE_SUBJECT_RULE = """SINGLE SUBJECT RULE (MANDATORY): this image model renders one character far better than several, so depict exactly ONE. Pick the most relevant subject of the latest scene -- the character the moment centers on (acting, speaking, or being acted upon) -- and tag only them: solo, one subject-count tag (1girl, 1boy, 1other -- or e621 style for anthro/feral subjects: anthro or feral plus male/female), then that character's appearance, pose, and expression. Never tag a second character's count or appearance; at most, imply others through the setting (a shadow, a doorway, an empty chair). A scene with no characters at all may be pure scenery (no humans)."""

# Danbooru-trained checkpoints (Illustrious 1.x+, NoobAI; Pony less so) can
# hold 2-3 distinct characters IF the prompt gives a correct subject-count tag
# combo and keeps each character's tags in one contiguous, non-interleaved
# group. This rule teaches the prompt writer that structure.
BOORU_MULTI_SUBJECT_RULE = """MULTI-SUBJECT STRUCTURE (MANDATORY): when the moment involves more than one character, structure the tag list so a tag-trained model keeps them distinct:
- Start with ONE correct subject-count tag combo: 2girls, 1boy 1girl, 2boys, 3girls, 2girls 1boy, 1girl 1other... (for anthro/feral characters use e621 style instead: duo or group plus anthro/feral and male/female). Count only the characters actually depicted, 3 at most -- if more are present, depict the 2-3 the moment centers on and fold the rest into the setting (crowd, blurry background figures).
- Then give EACH depicted character ONE CONTIGUOUS tag group, most central character first: lead with the traits that most distinguish them from the others in the image (hair color/length/style, eye color, species features like elf ears, horns, tail, fur), then outfit, then their own pose, expression, and action. NEVER interleave one character's traits with another's -- finish a character's group completely before starting the next.
- After the character groups, add interaction and placement tags that bind them together: side-by-side, facing another, looking at another, holding hands, hand on another's shoulder, hug, height difference...
- Finish with setting, lighting, mood, and composition tags as usual.
- Budget tags tightly: with several characters keep each group to roughly 6-10 tags, spending them on what tells the characters apart rather than generic detail.
- A scene that truly centers on one character may still be solo (solo, 1girl/1boy and that character's tags); a scene with none may be pure scenery (no humans)."""

BOORU_BREAK_RULE = ("- Put the single uppercase word BREAK between consecutive character tag "
                    "groups (its own item in the list, no commas attached to it).")

# Precomputed per-character appearance tags for tag-trained checkpoints. Each
# known character's sheet is distilled ONCE into canonical booru tags by a
# background LLM pass and cached (keyed by save + character, invalidated by a
# hash of the descriptor), so the prompt writer stops re-deriving -- and
# subtly re-inventing -- hair/eye colors on every image.
TAG_CACHE_FILE = "character_tags.json"
TAG_CACHE_MAX_SAVES = 20        # prune least-recently-updated saves beyond this
TAG_BACKFILL_MAX_PER_RUN = 10   # per-run cap; the next turn resumes the rest

# Scene-level and quality tags never belong in a character's canonical
# appearance; the cleaner drops them (plus score_* prefixes) even if the LLM
# slips, because a wrong cached tag would poison every future image.
TAG_OUTPUT_BLACKLIST = frozenset({
    "1girl", "1boy", "1other", "2girls", "2boys", "3girls", "3boys", "solo",
    "multiple girls", "multiple boys", "masterpiece", "best quality",
    "high quality", "highres", "absurdres",
})

CHARACTER_TAG_PROMPT = """You write booru tags (danbooru and e621 conventions) describing ONE character for an AI image generator. Distill the character sheet below into a comma-separated list of lowercase booru tags covering the character's PERMANENT PHYSICAL appearance ONLY.

Include (when stated or clearly implied): hair color, hair length and style, eye color, skin tone, species/race features (elf ears, horns, tail, wings, fur, scales, fangs), body build and height, age impression, and notable permanent marks (scars, tattoos, heterochromia, freckles). For anthro or feral characters use e621 conventions: anthro or feral, the species, and fur/scale/feather colors and markings.
STRICTLY EXCLUDE: clothing, armor, accessories, jewelry, weapons, held items, pose, expression, action, setting, lighting, quality tags (masterpiece, score_*...), and subject-count tags (1girl, 1boy, solo...).
If the sheet does not state hair color or eye color, infer a plausible choice from the character's race and overall description instead of omitting them -- your output becomes this character's canonical look.

CHARACTER SHEET:
Name: {name}
{descriptor}

Output ONLY the tag list, no quotes, no preamble, 5-15 tags."""

_services: dict = {}
_tasks: set = set()
_hf_detail_cache: dict = {}   # repo_id -> (fetched_at, detail json)
_tag_dict_cache: dict[str, int] | None = None   # tag/alias -> booru post count
_gen_lock: asyncio.Lock | None = None
_index_lock: asyncio.Lock | None = None
_lora_index_lock: asyncio.Lock | None = None
_tag_lock: asyncio.Lock | None = None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _data_dir() -> Path:
    base = _services.get("data_dir")
    if base:
        root = Path(base) / MODULE_ID
    else:
        root = Path(__file__).resolve().parent.parent.parent / "data" / MODULE_ID
    (root / "images").mkdir(parents=True, exist_ok=True)
    return root


def _default_config() -> dict:
    return {
        "enabled": False,
        "api_key": "",
        "model_name": "",               # a Novita checkpoint sd_name, picked via search
        "model_base": "",               # the picked model's base_model metadata (drives prompt style)
        "width": 1024,
        "height": 1024,
        "image_num": 1,                 # parallel images per generation, 1..IMAGE_NUM_MAX
        "steps": 28,
        "guidance_scale": 7.0,
        "sampler_name": "DPM++ 2M Karras",
        "negative_prompt": "blurry, low quality, watermark, text, deformed",
        "interval": 3,
        "step_retries": STEP_RETRIES_DEFAULT,
        "prompt_model_preference": "smartest",
        "prompt_template": DEFAULT_PROMPT_TEMPLATE,
        "prompt_template_tags": DEFAULT_PROMPT_TEMPLATE_TAGS,
        "pony_quality_tags": DEFAULT_PONY_QUALITY_TAGS,
        "booru_subject_mode": "auto",   # tag models: one of BOORU_SUBJECT_MODES
        "prompt_style_mode": "auto",    # one of PROMPT_STYLE_MODES
        "booru_break_separator": False, # multi mode: emit BREAK between character groups
        "tag_usage_filter": "off",      # one of TAG_USAGE_FILTER_MODES
        "tag_usage_min_count": DEFAULT_TAG_USAGE_MIN_COUNT,  # min booru posts to keep a tag
        "style_suffix": "",
        "character_reference_enabled": True,
        "player_in_images": "show",     # one of PLAYER_IN_IMAGES_MODES
        "chat_image_conceal": "off",    # one of CHAT_IMAGE_CONCEAL_MODES
        "civitai_api_key": "",
        "civitai_nsfw": "off",          # one of CIVITAI_NSFW_MODES
        "hf_api_key": "",               # optional; raises Hub rate limits
        "lora_library": [],             # saved LoRAs; see _normalize_lora_entry
    }


def _atomic_write_json(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _default_profile(name: str = "Default") -> dict:
    defaults = _default_config()
    return {"name": name, "lora_state": {},
            **{f: defaults[f] for f in PROFILE_FIELDS}}


def _default_store() -> dict:
    defaults = _default_config()
    return {
        "version": 2,
        **{f: defaults[f] for f in GLOBAL_FIELDS},
        "lora_library": [],
        "active_profile": "default",
        "profiles": {"default": _default_profile()},
    }


def _migrate_flat_store(stored: dict) -> dict:
    """v2 store from a pre-profile flat config: globals lift straight over,
    everything model-specific becomes the single "Default" profile, and each
    LoRA entry splits into shared metadata plus the profile's usage state.
    llm_mode is baked via _entry_llm_mode so pre-mode entries (condition text
    / legacy llm_weight flag) keep gating once the compose default of "off"
    exists."""
    store = _default_store()
    store.update({k: stored[k] for k in GLOBAL_FIELDS if k in stored})
    profile = store["profiles"]["default"]
    profile.update({k: stored[k] for k in PROFILE_FIELDS if k in stored})
    # booru_single_subject (bool) predates booru_subject_mode; keep the choice
    # the user made: True -> "single", False -> "multi" (they wanted several
    # characters and now get the structured multi rule).
    if "booru_subject_mode" not in stored \
            and isinstance(stored.get("booru_single_subject"), bool):
        profile["booru_subject_mode"] = \
            "single" if stored["booru_single_subject"] else "multi"
    library = []
    for entry in stored.get("lora_library") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        profile["lora_state"][str(entry["id"])] = {
            "active": bool(entry.get("active")),
            "strength": _clamp_lora_weight(entry.get("strength", LORA_DEFAULT_WEIGHT)),
            "llm_mode": _entry_llm_mode(entry),
            "condition": str(entry.get("condition") or ""),
        }
        library.append({k: v for k, v in entry.items()
                        if k not in LORA_STATE_FIELDS and k != "llm_weight"})
    store["lora_library"] = library
    return store


def _load_store() -> dict:
    """The raw on-disk store: globals + shared lora_library + profiles.
    Pre-profile flat files migrate in memory only (first save persists v2,
    same pattern as the legacy value migrations in _effective_config)."""
    path = _data_dir() / "config.json"
    stored = None
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[Image Gen] Failed to read config.json: {e}")
    if not isinstance(stored, dict):
        return _default_store()
    if not isinstance(stored.get("profiles"), dict) or not stored["profiles"]:
        return _migrate_flat_store(stored)

    store = _default_store()
    store.update({k: stored[k] for k in GLOBAL_FIELDS if k in stored})
    if isinstance(stored.get("lora_library"), list):
        store["lora_library"] = stored["lora_library"]
    profiles = {}
    for pid, raw in stored["profiles"].items():
        if not isinstance(raw, dict):
            continue
        profile = _default_profile(str(raw.get("name") or pid))
        profile.update({k: raw[k] for k in PROFILE_FIELDS if k in raw})
        if isinstance(raw.get("lora_state"), dict):
            profile["lora_state"] = raw["lora_state"]
        profiles[str(pid)] = profile
    if not profiles:
        profiles = {"default": _default_profile()}
    store["profiles"] = profiles
    active = str(stored.get("active_profile") or "")
    store["active_profile"] = active if active in profiles else next(iter(profiles))
    return store


def _save_store(store: dict) -> None:
    _atomic_write_json(_data_dir() / "config.json", store)


def _effective_config(store: dict) -> dict:
    """The flat config every consumer reads: globals + the active profile's
    fields, with lora_library entries overlaid with that profile's usage
    state. Entries are copies -- endpoints mutate them in place before
    _save_config decomposes the result back into the store."""
    cfg = _default_config()
    cfg.update({k: store[k] for k in GLOBAL_FIELDS if k in store})
    pid = store["active_profile"]
    profile = store["profiles"][pid]
    cfg.update({k: profile[k] for k in PROFILE_FIELDS if k in profile})
    cfg["active_profile"] = pid
    lora_state = profile.get("lora_state") or {}
    cfg["lora_library"] = [
        {**entry,
         "active": False, "strength": LORA_DEFAULT_WEIGHT,
         "llm_mode": "off", "condition": "",
         **{k: v for k, v in (lora_state.get(str(entry.get("id"))) or {}).items()
            if k in LORA_STATE_FIELDS}}
        for entry in store.get("lora_library") or []
        if isinstance(entry, dict)
    ]
    # civitai_nsfw was a bool before it became a mode string.
    if cfg.get("civitai_nsfw") not in CIVITAI_NSFW_MODES:
        cfg["civitai_nsfw"] = "include" if cfg.get("civitai_nsfw") is True else "off"
    if cfg.get("player_in_images") not in PLAYER_IN_IMAGES_MODES:
        cfg["player_in_images"] = "show"
    if cfg.get("chat_image_conceal") not in CHAT_IMAGE_CONCEAL_MODES:
        cfg["chat_image_conceal"] = "off"
    if cfg.get("booru_subject_mode") not in BOORU_SUBJECT_MODES:
        cfg["booru_subject_mode"] = "single"
    if cfg.get("prompt_style_mode") not in PROMPT_STYLE_MODES:
        cfg["prompt_style_mode"] = "auto"
    # A stored tags template that still equals an old default was never
    # customized; keep it tracking the current default.
    if cfg.get("prompt_template_tags") in LEGACY_PROMPT_TEMPLATES_TAGS:
        cfg["prompt_template_tags"] = DEFAULT_PROMPT_TEMPLATE_TAGS
    if cfg.get("tag_usage_filter") not in TAG_USAGE_FILTER_MODES:
        cfg["tag_usage_filter"] = "off"
    try:
        cfg["tag_usage_min_count"] = max(0, int(cfg.get("tag_usage_min_count")))
    except (TypeError, ValueError):
        cfg["tag_usage_min_count"] = DEFAULT_TAG_USAGE_MIN_COUNT
    try:
        cfg["step_retries"] = max(0, min(STEP_RETRIES_MAX, int(cfg.get("step_retries"))))
    except (TypeError, ValueError):
        cfg["step_retries"] = STEP_RETRIES_DEFAULT
    try:
        cfg["image_num"] = max(1, min(IMAGE_NUM_MAX, int(cfg.get("image_num"))))
    except (TypeError, ValueError):
        cfg["image_num"] = 1
    return cfg


def _load_config() -> dict:
    return _effective_config(_load_store())


def _save_config(cfg: dict) -> None:
    """Decompose an effective config back into the store: globals to the top
    level, profile fields to the profile the cfg was loaded under, lora usage
    state to that profile's lora_state, shared entry metadata to the library.
    active_profile itself is never written here -- only the activate endpoint
    moves it, so a writer holding a cfg loaded before a concurrent switch
    can't flip profiles (its edits still land in the profile it loaded).
    Never save a pipeline copy (e.g. _apply_lora_conditions output): its
    pruned lora_library would delete LoRAs from the shared catalog."""
    store = _load_store()
    pid = cfg.get("active_profile")
    if pid not in store["profiles"]:
        pid = store["active_profile"]
    profile = store["profiles"][pid]
    store.update({k: cfg[k] for k in GLOBAL_FIELDS if k in cfg})
    profile.update({k: cfg[k] for k in PROFILE_FIELDS if k in cfg})
    if isinstance(cfg.get("lora_library"), list):
        library = []
        lora_state = profile.get("lora_state")
        if not isinstance(lora_state, dict):
            lora_state = profile["lora_state"] = {}
        for entry in cfg["lora_library"]:
            if not isinstance(entry, dict):
                continue
            library.append({k: v for k, v in entry.items()
                            if k not in LORA_STATE_FIELDS})
            lora_state[str(entry.get("id"))] = {
                k: entry[k] for k in LORA_STATE_FIELDS if k in entry}
        store["lora_library"] = library
        # Deleted LoRAs leave every profile's state, not just the active one.
        ids = {str(e.get("id")) for e in library}
        for other in store["profiles"].values():
            state = other.get("lora_state")
            if isinstance(state, dict):
                other["lora_state"] = {i: s for i, s in state.items() if i in ids}
    _save_store(store)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return KEY_MASK_PREFIX + key[-4:]


def _get_index_lock() -> asyncio.Lock:
    global _index_lock
    if _index_lock is None:
        _index_lock = asyncio.Lock()
    return _index_lock


def _get_gen_lock() -> asyncio.Lock:
    global _gen_lock
    if _gen_lock is None:
        _gen_lock = asyncio.Lock()
    return _gen_lock


def _get_lora_index_lock() -> asyncio.Lock:
    global _lora_index_lock
    if _lora_index_lock is None:
        _lora_index_lock = asyncio.Lock()
    return _lora_index_lock


def _get_tag_lock() -> asyncio.Lock:
    global _tag_lock
    if _tag_lock is None:
        _tag_lock = asyncio.Lock()
    return _tag_lock


def _read_index() -> list[dict]:
    path = _data_dir() / "index.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
        return records if isinstance(records, list) else []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Image Gen] Failed to read index.json: {e}")
        return []


def _write_index(records: list[dict]) -> None:
    _atomic_write_json(_data_dir() / "index.json", records[-INDEX_MAX_RECORDS:])


async def _append_record(record: dict) -> None:
    async with _get_index_lock():
        records = _read_index()
        records.append(record)
        _write_index(records)


async def _patch_record(record_id: str, **fields) -> None:
    async with _get_index_lock():
        records = _read_index()
        for record in records:
            if record.get("id") == record_id:
                record.update(fields)
                break
        _write_index(records)


def _appearance_hash(descriptor: str) -> str:
    """Cache key for a character's canonical look. Hashing the full descriptor
    (gender + race + appearance text) means any change to any of the three
    invalidates the cached tags for free."""
    return hashlib.sha1(str(descriptor or "").encode("utf-8")).hexdigest()[:16]


def _read_tag_cache() -> dict:
    path = _data_dir() / TAG_CACHE_FILE
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        return cache if isinstance(cache, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Image Gen] Failed to read {TAG_CACHE_FILE}: {e}")
        return {}


def _write_tag_cache(cache: dict) -> None:
    if len(cache) > TAG_CACHE_MAX_SAVES:
        def newest(entries: dict) -> str:
            return max((str(e.get("updated_at") or "")
                        for e in entries.values() if isinstance(e, dict)),
                       default="")
        keep = sorted(cache, key=lambda s: newest(cache[s]))[-TAG_CACHE_MAX_SAVES:]
        cache = {s: cache[s] for s in keep}
    _atomic_write_json(_data_dir() / TAG_CACHE_FILE, cache)


def _clean_character_tags(raw: str, cfg: dict | None = None) -> str:
    """Normalized tag list from an LLM reply: lowercased, deduped, stripped of
    the scene-level/quality tags a character sheet must never carry, and (when
    cfg enables it) of tags too rare on the booru sites to render. Empty when the
    reply is unusable (caller skips caching, so it retries later)."""
    text = _clean_image_prompt(raw).lower()
    tags: list[str] = []
    seen: set[str] = set()
    for tag in text.split(","):
        tag = tag.strip()
        if not tag or tag in seen or tag in TAG_OUTPUT_BLACKLIST \
                or tag.startswith("score_"):
            continue
        seen.add(tag)
        tags.append(tag)
    cleaned = ", ".join(tags)
    if cleaned and cfg is not None:
        # Fail-open on full drop (inside the filter) keeps this from
        # returning "" and sending the backfill into a retry loop.
        cleaned = _filter_tags_by_usage(cleaned, cfg)
    return cleaned


def set_services(services: dict) -> None:
    """Capture shared engine services and clean up records that were mid-flight
    when the server last stopped, so the frontend never polls them forever."""
    global _services
    _services = services or {}
    try:
        records = _read_index()
        dirty = False
        for record in records:
            if record.get("status") in ("pending", "prompting", "generating"):
                record["status"] = "error"
                record["error"] = "interrupted by restart"
                dirty = True
        if dirty:
            _write_index(records)
    except Exception as e:
        print(f"[Image Gen] Startup index cleanup failed: {e}")


# --------------------------------------------------------------------------
# Prompt writing (LLM slot, never a concrete model)
# --------------------------------------------------------------------------

def _model_ident(cfg: dict) -> str:
    """Base-model metadata plus sd_name, for prompt-style detection. The name is
    included as a fallback for configs saved before model_base was stored."""
    return f"{cfg.get('model_base', '')} {cfg.get('model_name', '')}".lower()


# Substrings of Novita base_model / sd_name identifying danbooru-tag-trained
# checkpoints. "noob" (not "noobai") also matches "Noob AI" spellings, like
# _base_family below. Keep the mirror in ui/ImageStudio.jsx in sync.
BOORU_TAG_MODEL_MARKERS = ("pony", "illustrious", "noob", "animagine")


def _prompt_style(cfg: dict) -> str:
    """Resolved prompt style, "tags" (danbooru lists) or "natural" (descriptive
    text). An explicit prompt_style_mode wins; "auto" picks "tags" for
    Pony/Illustrious/NoobAI/Animagine bases and "natural" for Flux and
    everything else."""
    mode = str(cfg.get("prompt_style_mode") or "auto")
    if mode in ("tags", "natural"):
        return mode
    ident = _model_ident(cfg)
    if any(marker in ident for marker in BOORU_TAG_MODEL_MARKERS):
        return "tags"
    return "natural"


def _is_pony(cfg: dict) -> bool:
    return "pony" in _model_ident(cfg)


def _subject_mode(cfg: dict, characters: dict | None = None) -> str:
    """Resolved subject mode for tag-style prompts: "single" or "multi", or ""
    for natural-language models. "auto" resolves by how many tracked characters
    the scene roster puts in frame (the player counts unless POV hides them);
    untracked narration-only characters are invisible to it, so with no roster
    data it falls back to "single"."""
    if _prompt_style(cfg) != "tags":
        return ""
    mode = str(cfg.get("booru_subject_mode") or "single")
    if mode not in BOORU_SUBJECT_MODES:
        mode = "single"
    if mode != "auto":
        return mode
    count = 0
    if characters:
        pov = str(cfg.get("player_in_images") or "show") == "pov"
        if characters.get("player") and not pov:
            count += 1
        count += len(characters.get("npcs") or [])
    return "multi" if count >= 2 else "single"


# --------------------------------------------------------------------------
# LoRA library
# --------------------------------------------------------------------------

def _base_family(base: str) -> str:
    """Coarse base-model family for LoRA/checkpoint compatibility. SDXL-class
    covers everything trained on SDXL (Pony, Illustrious, NoobAI...)."""
    ident = str(base or "").lower()
    if "flux" in ident:
        return "flux"
    if "xl" in ident or "pony" in ident or "illustrious" in ident or "noob" in ident:
        return "sdxl"
    if "1.5" in ident or "sd 1" in ident or "sd1" in ident:
        return "sd15"
    return ""


def _checkpoint_family(cfg: dict) -> str:
    if cfg.get("model_name") == FLUX2_MODEL_NAME:
        return "flux"
    return _base_family(_model_ident(cfg))


def _entry_sd_name(entry: dict) -> str:
    """Novita library name for an SD-family LoRA: the hash-match result, or the
    user's manual override (for LoRAs console-uploaded to their account)."""
    override = str(entry.get("sd_name_override") or "").strip()
    if override:
        return override
    return str((entry.get("novita") or {}).get("sd_name_in_api") or "")


def _clamp_lora_weight(value, default: float = LORA_DEFAULT_WEIGHT) -> float:
    try:
        return round(max(LORA_WEIGHT_MIN, min(LORA_WEIGHT_MAX, float(value))), 2)
    except (TypeError, ValueError):
        return default


def _active_loras(cfg: dict) -> list[dict]:
    library = cfg.get("lora_library")
    if not isinstance(library, list):
        return []
    return [e for e in library if isinstance(e, dict) and e.get("active")]


def _entry_usable(entry: dict, family: str) -> bool:
    """Active entry is usable when it matches the checkpoint family and has a
    way to reach Novita (library name for SD, download URL for Flux)."""
    if _base_family(entry.get("base_model")) != family or not family:
        return False
    if family == "flux":
        return bool(str(entry.get("download_url") or "").strip())
    return bool(_entry_sd_name(entry))


def _sd_payload_loras(cfg: dict) -> list[dict]:
    family = _checkpoint_family(cfg)
    if family == "flux":
        return []
    out = []
    for entry in _active_loras(cfg):
        if not _entry_usable(entry, family):
            continue
        strength = _clamp_lora_weight(entry.get("strength", LORA_DEFAULT_WEIGHT))
        out.append({"model_name": _entry_sd_name(entry), "strength": strength})
    return out[:SD_LORAS_MAX]


def _lora_download_link(entry: dict, cfg: dict) -> str:
    """Download URL ready for a server-side fetch. Civitai requires the user's
    token appended (Novita downloads the file itself); other sources' URLs are
    used as-is — appending the token there would leak it to that host."""
    url = str(entry.get("download_url") or "").strip()
    if (entry.get("source") or "civitai") != "civitai" or "civitai.com" not in url:
        return url
    key = str(cfg.get("civitai_api_key") or "").strip()
    if url and key:
        url += ("&" if "?" in url else "?") + "token=" + key
    return url


def _flux_payload_loras(cfg: dict) -> list[str]:
    if _checkpoint_family(cfg) != "flux":
        return []
    urls = [
        _lora_download_link(entry, cfg)
        for entry in _active_loras(cfg)
        if _entry_usable(entry, "flux")
    ]
    return [u for u in urls if u][:FLUX_LORAS_MAX]


def _applied_lora_names(cfg: dict) -> list[str]:
    family = _checkpoint_family(cfg)
    return [
        str(entry.get("name") or entry.get("id") or "?")
        for entry in _active_loras(cfg)
        if _entry_usable(entry, family)
    ][:FLUX_LORAS_MAX if family == "flux" else SD_LORAS_MAX]


def _active_trigger_words(cfg: dict) -> list[str]:
    """Trained trigger words of the LoRAs that will actually be applied, so the
    prompt-writer LLM can work them in and the LoRAs fire."""
    family = _checkpoint_family(cfg)
    words: list[str] = []
    seen: set[str] = set()
    for entry in _active_loras(cfg):
        if not _entry_usable(entry, family):
            continue
        for word in (entry.get("trained_words") or [])[:4]:
            word = str(word).strip().strip(",")
            if word and word.lower() not in seen:
                seen.add(word.lower())
                words.append(word)
    return words[:12]


def _render_template(template: str, narration: str, history: str) -> str:
    # Sequential replace instead of str.format: narration prose routinely
    # contains braces that would blow up format().
    out = template.replace("{narration}", narration)
    out = out.replace("{history}", history or "(story just began)")
    return out


def _clean_image_prompt(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        first_line, _, rest = text.partition("\n")
        if first_line.strip().lower() in ("text", "prompt", "markdown"):
            text = rest
    text = text.strip().strip('"').strip()
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_PROMPT_CHARS]


def _tag_usage_dict() -> dict[str, int] | None:
    """Lazy-loaded tag -> post count map merged from the bundled booru CSVs
    (danbooru + e621), with aliases resolving to their canonical tag's count
    and a tag known to several sites keeping its highest count. None when no
    file is readable (callers fail open and keep the prompt unfiltered).
    Cached for the process lifetime, including the failure."""
    global _tag_dict_cache
    if _tag_dict_cache is not None:
        return _tag_dict_cache or None
    merged: dict[str, int] = {}
    for path in TAG_DICT_FILES:
        counts: dict[str, int] = {}
        aliases: dict[str, int] = {}
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                for row in csv.reader(f):
                    try:
                        name, count = row[0].strip(), int(row[2])
                    except (IndexError, ValueError):
                        continue
                    if not name:
                        continue
                    counts[name] = count
                    if len(row) > 3 and row[3]:
                        for alias in row[3].split(","):
                            alias = alias.strip()
                            if alias:
                                aliases.setdefault(alias, count)
        except OSError as e:
            print(f"[Image Gen] Tag dictionary {path.name} unavailable: {e}")
            continue
        # Canonical names win over any alias spelled the same way; across
        # sites a tag keeps its highest count.
        for key, count in {**aliases, **counts}.items():
            if count > merged.get(key, -1):
                merged[key] = count
    if not merged:
        print("[Image Gen] No tag dictionary readable, usage filter disabled")
    _tag_dict_cache = merged
    return _tag_dict_cache or None


def _normalize_tag_for_lookup(token: str) -> str:
    """Dictionary lookup key for a prompt token: lowercased, attention-weight
    syntax stripped ((long hair:1.2) -> long hair), escaped parens unescaped,
    whitespace collapsed to underscores. Lookup-only -- the caller re-emits
    the original token text. Legitimate trailing parens (sword_(weapon))
    are kept."""
    t = token.strip().lower().replace("\\(", "(").replace("\\)", ")")
    m = re.fullmatch(r"\((.+?):-?\d+(?:\.\d+)?\)", t)
    if m:
        t = m.group(1)
    else:
        t = re.sub(r":-?\d+(?:\.\d+)?$", "", t)
    return re.sub(r"\s+", "_", t.strip())


def _filter_tags_by_usage(tag_text: str, cfg: dict,
                          whitelist: tuple | list = ()) -> str:
    """tag_text with low-usage booru tags removed per cfg's
    tag_usage_filter mode ("soft" drops known tags below
    tag_usage_min_count; "hard" also drops unknown tags). A tag exactly at
    the threshold is kept. Preserves token order and original spelling,
    BREAK separators, score_* tags, and whitelisted trigger words. Fails
    open -- returns tag_text unchanged -- when the mode is off, the
    dictionaries are unavailable, or filtering would drop every tag."""
    mode = cfg.get("tag_usage_filter")
    if mode not in ("soft", "hard"):
        return tag_text
    usage = _tag_usage_dict()
    if usage is None:
        return tag_text
    try:
        threshold = max(0, int(cfg.get("tag_usage_min_count")))
    except (TypeError, ValueError):
        threshold = DEFAULT_TAG_USAGE_MIN_COUNT
    keep_always = {_normalize_tag_for_lookup(part)
                   for word in whitelist for part in str(word).split(",")
                   if part.strip()}
    kept_tokens: list[str] = []
    dropped: list[str] = []
    real_tags_kept = 0
    for token in tag_text.split(","):
        # BREAK may sit inside a comma token ("red hair BREAK 1boy");
        # filter around it and re-emit it verbatim.
        kept_parts: list[str] = []
        for part in re.split(r"\b(BREAK)\b", token):
            piece = part.strip()
            if not piece:
                continue
            if piece == "BREAK":
                kept_parts.append(piece)
                continue
            key = _normalize_tag_for_lookup(piece)
            if not key:
                continue
            if key.startswith("score_") or key in keep_always:
                kept_parts.append(piece)
                real_tags_kept += 1
                continue
            count = usage.get(key)
            if count is None:
                if mode == "hard":
                    dropped.append(piece)
                    continue
            elif count < threshold:
                dropped.append(piece)
                continue
            kept_parts.append(piece)
            real_tags_kept += 1
        if kept_parts:
            kept_tokens.append(" ".join(kept_parts))
    if not dropped:
        return tag_text
    if not real_tags_kept:
        print(f"[Image Gen] Tag filter ({mode}, min {threshold}) would drop "
              f"every tag; keeping prompt unfiltered")
        return tag_text
    print(f"[Image Gen] Tag filter ({mode}, min {threshold}) dropped: "
          f"{', '.join(dropped)}")
    return ", ".join(kept_tokens)


def _parse_condition_reply(raw: str) -> dict[int, float | None] | None:
    """Adapter verdicts from an LLM reply: {number: weight} from a JSON
    object, or {number: None} from a bare number array (weight unspecified,
    keep the configured one). None when neither can be found (callers fail
    open)."""
    m = re.search(r"\{[^{}]*\}", raw or "")
    if m:
        try:
            parsed = json.loads(m.group(0))
            return {int(k): _clamp_lora_weight(v, None) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    m = re.search(r"\[[\d,\s]*\]", raw or "")
    if not m:
        return None
    try:
        return {int(n): None for n in json.loads(m.group(0))}
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _entry_llm_mode(entry: dict) -> str:
    """Effective AI mode for a library entry: 'gate' (the condition decides
    whether it applies), 'weight' (the LLM picks the strength, guided by the
    instructions), 'both', or 'off'. Entries saved before explicit modes
    derive it from their fields (condition text meant gating, the llm_weight
    flag meant weight control). Gating without condition text has nothing to
    decide, so it degrades to 'off' / 'weight'."""
    mode = entry.get("llm_mode")
    has_text = bool(str(entry.get("condition") or "").strip())
    if mode not in LORA_LLM_MODES:
        if entry.get("llm_weight"):
            mode = "both" if has_text else "weight"
        else:
            mode = "gate" if has_text else "off"
    if mode in ("gate", "both") and not has_text:
        mode = "off" if mode == "gate" else "weight"
    return mode


def _condition_line(n: int, entry: dict) -> str:
    """One adapter line for the gate prompt, opening with the mode label the
    prompt header defines so the LLM never has to infer which regime an
    adapter is under. The condition text is uncapped: it only feeds LLM input
    (see CLAUDE.md -- no token caps on LLM input context)."""
    mode = _entry_llm_mode(entry)
    cond = str(entry.get("condition") or "").strip()
    weight = _clamp_lora_weight(entry.get("strength", LORA_DEFAULT_WEIGHT))
    if mode == "gate":
        return f"{n}. [GATED, weight {weight}] condition: {cond}"
    if mode == "weight":
        line = f"{n}. [ALWAYS APPLIES, pick the weight, default {weight}]"
        return f"{line} instructions: {cond}" if cond else line
    return f"{n}. [GATED, pick the weight if it applies, default {weight}] condition: {cond}"


def _condition_character_block(characters: dict | None) -> str:
    """Character sheets for the gate prompt, so conditions and weight
    instructions can reference who is present even when the narration only
    uses pronouns or epithets. Lists every known living character -- not just
    those judged present in the scene -- because a per-character LoRA
    condition must be able to match anyone the narration might involve."""
    if not characters:
        return ""
    lines = []
    player = characters.get("player")
    if player:
        lines.append(f"- {player['name']} (player character): {player['descriptor']}")
    for npc in characters.get("all_npcs") or characters.get("npcs") or []:
        lines.append(f"- {npc['name']}: {npc['descriptor']}")
    if not lines:
        return ""
    return "\n\nCHARACTERS PRESENT (canonical sheets):\n" + "\n".join(lines)


async def _apply_lora_conditions(cfg: dict, narration: str, sdk,
                                 characters: dict | None = None) -> dict:
    """A cfg copy whose lora_library reflects the scene as judged by the
    fastest LLM slot: active conditional LoRAs it deems irrelevant are
    dropped, and weight-controlled LoRAs get the weight it picked (an LLM
    weight of 0 also drops the LoRA). Character sheets ride along so
    conditions can reference who is present. Any failure (no LLM, LLM error,
    unparseable reply) fails open: every active LoRA stays at its configured
    strength, same as before conditions existed."""
    participating = [
        e for e in _active_loras(cfg) if _entry_llm_mode(e) != "off"
    ]
    if not participating or sdk is None:
        return cfg
    lines = "\n".join(
        _condition_line(i + 1, e) for i, e in enumerate(participating))
    prompt = (LORA_CONDITION_PROMPT
              .replace("{narration}", (narration or "")[-3000:])
              .replace("{characters}", _condition_character_block(characters))
              .replace("{conditions}", lines))
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference="fastest")
    except Exception as e:
        print(f"[Image Gen] LoRA condition check failed (keeping all): {e}")
        return cfg
    finally:
        sdk.llm._current_module = ""
    verdicts = _parse_condition_reply(raw)
    if verdicts is None:
        print(f"[Image Gen] Unparseable LoRA condition reply (keeping all): {raw[:200]!r}")
        return cfg

    rejected: list[dict] = []
    weights: dict = {}  # entry id -> LLM-picked strength for this image
    for i, e in enumerate(participating):
        mode = _entry_llm_mode(e)
        if (i + 1) not in verdicts:
            # Weight-only entries always apply; an omission there is an LLM
            # slip, so they fail open to their configured strength.
            if mode != "weight":
                rejected.append(e)
            continue
        weight = verdicts[i + 1]
        if mode != "gate" and weight is not None:
            if abs(weight) < 0.005:
                rejected.append(e)
            else:
                weights[e.get("id")] = weight
    if not rejected and not weights:
        return cfg
    rejected_ids = {e.get("id") for e in rejected}
    library = []
    for e in cfg.get("lora_library") or []:
        if isinstance(e, dict) and e.get("id") in rejected_ids:
            continue
        if isinstance(e, dict) and e.get("id") in weights:
            e = {**e, "strength": weights[e.get("id")]}
        library.append(e)
    if rejected:
        skipped = ", ".join(str(e.get("name") or e.get("id")) for e in rejected)
        print(f"[Image Gen] Conditions skipped LoRAs for this scene: {skipped}")
    if weights:
        picked = ", ".join(
            f"{e.get('name') or e.get('id')}={weights[e.get('id')]}"
            for e in participating if e.get("id") in weights)
        print(f"[Image Gen] LLM-picked LoRA weights for this scene: {picked}")
    return {**cfg, "lora_library": library}


def _character_block(cfg: dict, characters: dict | None,
                     subject_mode: str | None = None) -> str:
    """Instruction block pinning known characters to their canonical
    appearances (and, in POV mode, switching to first-person only when the
    player is directly interacting with someone). Rides the prompt writer's
    INPUT, so it does not eat into the MAX_PROMPT_CHARS output cap."""
    if not characters or not cfg.get("character_reference_enabled", True):
        return ""
    if subject_mode is None:
        subject_mode = _subject_mode(cfg, characters)
    pov = str(cfg.get("player_in_images") or "show") == "pov"
    tags = _prompt_style(cfg) == "tags"

    def _line(sheet: dict, label_suffix: str = "") -> str:
        # A sheet with precomputed appearance tags serves them ready-made;
        # the description rides along because clothing is deliberately not
        # part of the canonical tags.
        label = f"{sheet['name']}{label_suffix}"
        if tags and sheet.get("tags"):
            return (f"- {label}: appearance tags (canonical, use verbatim): "
                    f"{sheet['tags']} | description: {sheet['descriptor']}")
        return f"- {label}: {sheet['descriptor']}"

    lines = []
    any_tagged = False
    player = characters.get("player")
    if player and not pov:
        lines.append(_line(player, " (player character)"))
        any_tagged = any_tagged or (tags and bool(player.get("tags")))
    for npc in characters.get("npcs") or []:
        lines.append(_line(npc))
        any_tagged = any_tagged or (tags and bool(npc.get("tags")))

    parts = []
    if lines:
        if tags and subject_mode == "single":
            header = ("KNOWN CHARACTERS -- canonical appearances (MANDATORY): if the ONE "
                      "subject you depict is listed below, convert their description into "
                      "concrete booru appearance tags (hair, eyes, skin, clothing, species, "
                      "distinctive features) and include those tags. Stay faithful to the "
                      "description -- never invent or contradict a listed trait; a subject "
                      "not listed may be described freely.")
        elif tags:
            header = ("KNOWN CHARACTERS -- canonical appearances (MANDATORY): every character "
                      "you depict who is listed below MUST get their own contiguous tag group. "
                      "Convert each one's description into concrete booru appearance tags "
                      "(hair, eyes, skin, clothing, species, distinctive features), leading "
                      "each group with the traits that most set that character apart from the "
                      "others in the image. Never merge two characters' traits into one group. "
                      "Stay faithful to the descriptions -- never invent or contradict a "
                      "listed trait; characters not listed may be described freely.")
        else:
            header = ("KNOWN CHARACTERS -- canonical appearances (MANDATORY): when any of these "
                      "characters appears in the scene, depict them EXACTLY as described below. "
                      "Never invent, change, or contradict a listed trait; characters not "
                      "listed may be described freely.")
        if any_tagged:
            header += (" Characters listed with 'appearance tags' already have their "
                       "permanent physical traits as ready booru tags: include those tags "
                       "VERBATIM for that character (never re-derive, drop, or contradict "
                       "them), and take only clothing, pose, and expression from their "
                       "description and the scene.")
        parts.append(header + "\n" + "\n".join(lines))
    if pov:
        # The player is never depicted in POV mode. The first-person camera,
        # though, is a last resort -- forcing every scene through the player's
        # eyes is disorienting -- so it is reserved for the moments that truly
        # need it: the player in direct, physical contact with someone (an
        # embrace, close combat, sex). Ordinary scenes simply leave the player
        # out of frame with no forced viewpoint.
        rule = ("POV RULE (MANDATORY): never depict the player character -- no face, no "
                "body; keep them out of frame. ONLY when the scene shows the player in "
                "direct, physical interaction with another character -- an embrace, close "
                "combat, sex, or similar close contact -- render it in first person through "
                "the player's own eyes (at most their hands at the frame edge). In every "
                "other scene, simply frame it so the player is absent, with no forced "
                "first-person viewpoint.")
        if tags:
            rule += " When first person applies, include framing tags such as pov."
        parts.append(rule)
    return "".join("\n\n" + p for p in parts)


async def _write_image_prompt(cfg: dict, narration: str, history: str, sdk,
                              characters: dict | None = None,
                              variation_hint: str = "",
                              insist: bool = False) -> str:
    style = _prompt_style(cfg)
    if style == "tags":
        template = cfg.get("prompt_template_tags") or DEFAULT_PROMPT_TEMPLATE_TAGS
    else:
        template = cfg.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE
    prompt = _render_template(template, narration[-4000:], history[-3000:])
    subject_mode = _subject_mode(cfg, characters)
    if subject_mode == "single":
        prompt += "\n\n" + BOORU_SINGLE_SUBJECT_RULE
    elif subject_mode == "multi":
        rule = BOORU_MULTI_SUBJECT_RULE
        if cfg.get("booru_break_separator"):
            rule += "\n" + BOORU_BREAK_RULE
        prompt += "\n\n" + rule
    triggers = _active_trigger_words(cfg)
    if triggers:
        prompt += ("\n\nMANDATORY: weave these trigger words into the output verbatim "
                   "(they activate style adapters): " + ", ".join(triggers))
    prompt += _character_block(cfg, characters, subject_mode)
    if variation_hint:
        prompt += ("\n\nVARIATION: several prompts are being written "
                   "independently for this same scene, and this one must "
                   "stand apart. " + variation_hint)
    if insist:
        # Only added on retries after the writer refused: the first attempt
        # keeps the instructions exactly as configured.
        prompt += ("\n\nREMINDER: You are writing an image-generation prompt "
                   "for a scene in a FICTIONAL interactive story. Nothing "
                   "real is depicted. Output ONLY the prompt text — never "
                   "refuse and never add commentary.")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(
            prompt, model_preference=cfg.get("prompt_model_preference", "smartest"))
    finally:
        sdk.llm._current_module = ""
    image_prompt = _clean_image_prompt(raw)
    if not image_prompt:
        raise RuntimeError("prompt writer returned an empty prompt")
    # A refusal ("I'm sorry, I can't help with that.") must not become the
    # image prompt. Plain RuntimeError so the step retries re-ask, with the
    # fiction reminder added on those retries.
    if _looks_like_llm_refusal(image_prompt):
        raise RuntimeError(f"prompt writer refused: {image_prompt[:120]}")
    if style == "tags":
        image_prompt = _filter_tags_by_usage(image_prompt, cfg, whitelist=triggers)

    # Pony checkpoints are trained to expect score_* quality tags up front.
    prefix = str(cfg.get("pony_quality_tags") or "").strip() if _is_pony(cfg) else ""
    suffix = str(cfg.get("style_suffix") or "").strip()

    # Trim the scene text, never the prefix/suffix, to fit Novita's cap.
    reserved = (len(prefix) + 2 if prefix else 0) + (len(suffix) + 2 if suffix else 0)
    image_prompt = image_prompt[:max(0, MAX_PROMPT_CHARS - reserved)].rstrip(", ")
    pieces = [p for p in (prefix, image_prompt, suffix) if p]
    return ", ".join(pieces)[:MAX_PROMPT_CHARS]


async def _soften_image_prompt(cfg: dict, refused_prompt: str, reason: str,
                               sdk) -> str:
    """Rewrite a prompt the provider's content filter rejected: same scene
    and format, flaggable content toned down. Raises when the rewrite itself
    is unusable, which surfaces as a normal retryable step failure."""
    ask = (
        "The following AI image prompt was rejected by the image provider's "
        "content filter. Rewrite it so it passes review: keep the same "
        "scene, subjects, and composition, and keep the same format (a "
        "comma-separated tag list stays a tag list; natural language stays "
        "natural language), but remove or tone down whatever a content "
        "filter would flag (explicit gore, sexual content, graphic "
        "violence...).\n\n"
        f"REJECTION REASON:\n{(reason or '').strip()[:300]}\n\n"
        f"PROMPT:\n{refused_prompt}\n\n"
        "Output ONLY the rewritten prompt, no quotes, no preamble."
    )
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(
            ask, model_preference=cfg.get("prompt_model_preference", "smartest"))
    finally:
        sdk.llm._current_module = ""
    softened = _clean_image_prompt(raw)
    if not softened or _looks_like_llm_refusal(softened):
        raise RuntimeError("prompt softener returned no usable prompt")
    return softened[:MAX_PROMPT_CHARS]


# --------------------------------------------------------------------------
# Novita client
# --------------------------------------------------------------------------

def _novita_headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_key']}", "accept": "application/json"}


def _novita_payload(cfg: dict, image_prompt: str) -> dict:
    payload = {
        "extra": {"response_image_type": "jpeg"},
        "request": {
            "model_name": str(cfg.get("model_name", "")),
            "prompt": image_prompt[:MAX_PROMPT_CHARS],
            "width": int(cfg.get("width", 1024)),
            "height": int(cfg.get("height", 1024)),
            "image_num": 1,
            "steps": int(cfg.get("steps", 28)),
            "guidance_scale": float(cfg.get("guidance_scale", 7.0)),
            "sampler_name": str(cfg.get("sampler_name", "DPM++ 2M Karras")),
            "seed": -1,
        },
    }
    negative = str(cfg.get("negative_prompt") or "").strip()
    if negative:
        payload["request"]["negative_prompt"] = negative[:MAX_PROMPT_CHARS]
    loras = _sd_payload_loras(cfg)
    if loras:
        payload["request"]["loras"] = loras
    return payload


def _flux2_payload(cfg: dict, image_prompt: str) -> dict:
    clamp = lambda v: max(FLUX2_SIZE_MIN, min(FLUX2_SIZE_MAX, int(v or 1024)))
    payload = {
        "prompt": image_prompt[:MAX_PROMPT_CHARS],
        "size": f"{clamp(cfg.get('width'))}*{clamp(cfg.get('height'))}",
        "seed": -1,
    }
    loras = _flux_payload_loras(cfg)
    if loras:
        payload["loras"] = loras
    return payload


class NonRetryableError(RuntimeError):
    """A failure that would recur identically if the step were re-run with the
    same input — an invalid API key, rejected request parameters, or a
    content-policy refusal. Step retries re-raise it immediately instead of
    burning attempts (and Novita credits) on a lost cause."""


class ProviderRefusal(NonRetryableError):
    """The image provider's content filter rejected the prompt. Resubmitting
    the identical prompt is pointless (hence NonRetryable), but the pipeline
    catches this specifically and retries with an LLM-softened rewrite when
    the prompt was LLM-written in the first place."""


# Markers that identify a provider content-policy refusal (as opposed to a
# bad-parameter or quota error) in Novita's `reason`/`message` text.
_REFUSAL_MARKERS = (
    "content policy", "policy", "moderat", "sensitive", "nsfw", "safety",
    "prohibit", "not allowed", "violat", "forbidden", "flagged",
)


def _looks_like_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _REFUSAL_MARKERS)


# Refusal openers from the prompt-writer LLM ("I'm sorry, I can't help with
# that."). Checked only against the start of the reply: real scene prompts
# and booru tag lists never open this way, which keeps false positives out.
_LLM_REFUSAL_MARKERS = (
    "i'm sorry", "i am sorry", "i apologize", "i can't", "i cannot",
    "i won't", "can't help", "cannot help", "can't assist", "cannot assist",
    "can't create", "cannot create", "unable to assist", "not able to help",
    "as an ai", "i must decline", "against my guidelines",
)


def _looks_like_llm_refusal(text: str) -> bool:
    # Refusals open with these phrases, so a short window is enough — and it
    # keeps phrases like "a soldier who cannot help but weep" deeper inside a
    # legitimate prompt from being mistaken for one.
    head = (text or "").strip().lower().replace("’", "'")[:48]
    return any(m in head for m in _LLM_REFUSAL_MARKERS)


def _novita_error_detail(resp) -> str:
    """Flatten Novita's error envelope into one legible line.

    Novita wraps errors as ``{code, reason, message, metadata}``: ``message``
    is human-readable, ``reason`` is a stable code (content-policy tags land
    here), and ``metadata`` carries extra context. The old version returned
    only the first of ``message``/``reason`` it found and dropped the rest, so
    a refusal whose signal lived in ``reason`` or ``metadata`` surfaced as a
    vague message. Surface all three."""
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "").strip()[:300] or f"HTTP {resp.status_code}"
    if not isinstance(body, dict):
        return str(body)[:300]
    message = str(body.get("message") or "").strip()
    reason = str(body.get("reason") or "").strip()
    parts = [message] if message else []
    # The reason code adds signal only when it isn't already echoed by message.
    if reason and reason.lower() not in message.lower():
        parts.append(f"[{reason}]")
    meta = body.get("metadata")
    if isinstance(meta, dict):
        extra = "; ".join(
            f"{k}: {v}" for k, v in meta.items()
            if str(v).strip() and str(v).strip().lower() not in message.lower())
        if extra:
            parts.append(f"({extra})")
    return (" ".join(parts).strip() or str(body))[:300]


def _describe_novita_failure(detail: str, status_code: int | None = None) -> str:
    """Turn a raw Novita error detail into the message we store on the record.
    Content-policy refusals get a plain-language prefix; everything else keeps
    the HTTP context so genuine request errors stay diagnosable."""
    detail = (detail or "").strip() or "no detail given"
    if _looks_like_refusal(detail):
        return f"The image provider refused this prompt (content policy): {detail}"
    if status_code is not None:
        return f"Novita rejected the request (HTTP {status_code}): {detail}"
    return f"Novita generation failed: {detail}"


async def _novita_submit(cfg: dict, image_prompt: str) -> str:
    """Submit an async generation task; return the task id. FLUX.2 is a
    first-party model on its own endpoint; everything else is SD txt2img."""
    import httpx
    if cfg.get("model_name") == FLUX2_MODEL_NAME:
        url = f"{NOVITA_BASE}/v3/async/flux-2-dev"
        payload = _flux2_payload(cfg, image_prompt)
    else:
        url = f"{NOVITA_BASE}/v3/async/txt2img"
        payload = _novita_payload(cfg, image_prompt)
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for attempt in range(SUBMIT_RETRIES + 1):
            try:
                resp = await client.post(url, headers=_novita_headers(cfg), json=payload)
                if resp.status_code in (401, 403):
                    raise NonRetryableError(
                        f"Novita rejected the request ({resp.status_code}): invalid API key")
                if resp.status_code in (400, 402, 422):
                    detail = _describe_novita_failure(
                        _novita_error_detail(resp), resp.status_code)
                    cls = ProviderRefusal if _looks_like_refusal(detail) else NonRetryableError
                    raise cls(detail)
                if resp.status_code == 429:
                    # Rate limiting clears with time, so the step retry may
                    # succeed — unlike the parameter/quota rejections above.
                    raise RuntimeError(
                        _describe_novita_failure(_novita_error_detail(resp), resp.status_code))
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Novita server error {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                task_id = (resp.json() or {}).get("task_id")
                if not task_id:
                    raise RuntimeError(f"Novita response missing task_id: {resp.text[:300]}")
                return task_id
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_error = e
                if attempt < SUBMIT_RETRIES:
                    await asyncio.sleep(2 + attempt * 3)
    raise RuntimeError(f"Novita submit failed after {SUBMIT_RETRIES + 1} attempts: {last_error}")


async def _novita_poll(cfg: dict, task_id: str) -> str:
    """Poll the task until it succeeds; return the presigned image URL."""
    import httpx
    url = f"{NOVITA_BASE}/v3/async/task-result"
    transient_failures = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for _ in range(POLL_MAX_ITERATIONS):
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                resp = await client.get(url, headers=_novita_headers(cfg),
                                        params={"task_id": task_id})
                resp.raise_for_status()
                body = resp.json()
                transient_failures = 0
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                transient_failures += 1
                if transient_failures > POLL_MAX_TRANSIENT_FAILURES:
                    raise RuntimeError(f"Novita polling kept failing: {e}")
                continue

            task = body.get("task") or {}
            status = str(task.get("status", ""))
            if status == "TASK_STATUS_SUCCEED":
                images = body.get("images") or []
                image_url = images[0].get("image_url") if images else None
                if not image_url:
                    raise RuntimeError("Novita task succeeded but returned no image URL")
                return image_url
            if status == "TASK_STATUS_FAILED":
                # V3 async reports `reason`; older/other endpoints use
                # `failed_reason` — check both so nothing collapses to blank.
                reason = str(task.get("reason") or task.get("failed_reason")
                             or "no reason given").strip()[:300]
                # A refusal would refuse the same prompt again; any other task
                # failure (worker crash, capacity) is worth a resubmission.
                cls = ProviderRefusal if _looks_like_refusal(reason) else RuntimeError
                raise cls(_describe_novita_failure(reason))
            # TASK_STATUS_QUEUED / TASK_STATUS_PROCESSING: keep polling.
    raise RuntimeError("Novita generation timed out")


async def _download(image_url: str) -> tuple[bytes, str]:
    """Download the presigned result immediately (the URL expires)."""
    import httpx
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for attempt in range(2):
            try:
                resp = await client.get(image_url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                ext = {"image/png": "png", "image/webp": "webp"}.get(
                    content_type.split(";")[0].strip(), "jpg")
                return resp.content, ext
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_error = e
                if attempt == 0:
                    await asyncio.sleep(2)
    raise RuntimeError(f"Image download failed: {last_error}")


async def _novita_list_models(cfg: dict, query: str, cursor: str, limit: int,
                              types: str = "checkpoint",
                              visibility: str = "") -> dict:
    """Search Novita's model catalog (thousands of Civitai-mirrored models).
    visibility="private" lists the account's own console-uploaded models."""
    import httpx
    params = {
        "filter.types": types,
        "pagination.limit": max(1, min(100, limit)),
    }
    if visibility:
        params["filter.visibility"] = visibility
    if query:
        params["filter.query"] = query
    if cursor:
        params["pagination.cursor"] = cursor

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        resp = await client.get(f"{NOVITA_BASE}/v3/model",
                                headers=_novita_headers(cfg), params=params)
        if resp.status_code in (401, 403):
            raise RuntimeError("Novita rejected the model search: invalid API key")
        if resp.status_code != 200:
            raise RuntimeError(f"Novita model search failed ({resp.status_code}): "
                               f"{_novita_error_detail(resp)}")
        return resp.json()


def _public_checkpoints(body: dict) -> list[dict]:
    """Reduce a /v3/model response to the dropdown-entry shape, dropping
    models that are not deployable (status != 1) or have no usable sd_name."""
    return [
        {
            "sd_name": m.get("sd_name_in_api") or m.get("sd_name"),
            "name": m.get("name"),
            "is_sdxl": bool(m.get("is_sdxl")),
            "base_model": m.get("base_model"),
            "cover_url": m.get("cover_url"),
        }
        for m in (body.get("models") or [])
        if m.get("status") == 1 and (m.get("sd_name_in_api") or m.get("sd_name"))
    ]


def _novita_query_variants(query: str) -> list[str]:
    """Alternate spellings for a multi-word catalog query. Novita names its
    Civitai mirrors after the *file* name — camelCase, no spaces (the model
    titled "Jib Mix Realistic XL" is "jibMixRealisticXL_v10_...") — so a
    page-title query with spaces misses. Only spellings that differ from the
    original are returned, so single-word queries produce none."""
    words = query.split()
    if len(words) < 2:
        return []
    collapsed = "".join(words)
    camel = words[0].lower() + "".join(w[:1].upper() + w[1:] for w in words[1:])
    variants: list[str] = []
    for v in (collapsed, camel, collapsed.lower()):
        if v != query and v not in variants:
            variants.append(v)
    return variants


def _query_tokens(query: str) -> list[str]:
    """Alphanumeric words of a query, deduped case-insensitively, in order."""
    tokens: list[str] = []
    for t in re.findall(r"[A-Za-z0-9]{2,}", query):
        if t.casefold() not in {x.casefold() for x in tokens}:
            tokens.append(t)
    return tokens


async def _novita_search_fallback(cfg: dict, query: str,
                                  limit: int) -> tuple[str, list[dict], str]:
    """Recover from a zero-hit catalog search caused by spacing/word order.
    First retries no-space spellings of the query (see _novita_query_variants);
    the winning spelling is returned as the effective query so the client can
    keep paginating with it. If those miss too, searches word-by-word and
    keeps only models whose name contains every word of the original query —
    that path post-filters, so its Novita cursor would leak unfiltered pages
    and pagination is disabled instead.
    Returns (effective_query, models, next_cursor)."""
    for variant in _novita_query_variants(query):
        body = await _novita_list_models(cfg, variant, "", limit)
        models = _public_checkpoints(body)
        if models:
            next_cursor = (body.get("pagination") or {}).get("next_cursor") or ""
            return variant, models, next_cursor
    tokens = _query_tokens(query)
    if len(tokens) >= 2:
        for token in sorted(tokens, key=len, reverse=True)[:3]:
            body = await _novita_list_models(cfg, token, "", 100)
            models = [
                m for m in _public_checkpoints(body)
                if all(t.casefold() in f"{m['name']} {m['sd_name']}".casefold()
                       for t in tokens)
            ]
            if models:
                return query, models, ""
    return query, [], ""


# --------------------------------------------------------------------------
# Civitai client (LoRA browsing) + Novita availability matching
# --------------------------------------------------------------------------

def _civitai_headers(cfg: dict) -> dict:
    headers = {"accept": "application/json"}
    key = str(cfg.get("civitai_api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _flatten_civitai_model(model: dict) -> dict | None:
    """Reduce a Civitai /models hit to the library-entry shape (latest version,
    primary file). Returns None for hits without a downloadable version."""
    versions = model.get("modelVersions") or []
    version = versions[0] if versions else {}
    if not version.get("id"):
        return None
    files = version.get("files") or []
    file = next((f for f in files if f.get("primary")), files[0] if files else {})
    # Novita may mirror an older version, so keep every version's file hash
    # for availability matching (latest first, like modelVersions).
    all_hashes: list[str] = []
    for v in versions[:10]:
        vfiles = v.get("files") or []
        vfile = next((f for f in vfiles if f.get("primary")), vfiles[0] if vfiles else {})
        vhash = str((vfile.get("hashes") or {}).get("SHA256") or "").lower()
        if vhash and vhash not in all_hashes:
            all_hashes.append(vhash)
    thumb = next(
        (i.get("url") for i in (version.get("images") or [])
         if i.get("url") and i.get("type") != "video"),
        "")
    stats = model.get("stats") or {}
    return {
        "id": str(version["id"]),
        "model_id": model.get("id"),
        "name": str(model.get("name") or ""),
        "version_name": str(version.get("name") or ""),
        "creator": str((model.get("creator") or {}).get("username") or ""),
        "type": str(model.get("type") or "LORA"),
        "base_model": str(version.get("baseModel") or ""),
        "sha256": str((file.get("hashes") or {}).get("SHA256") or "").lower(),
        "all_hashes": all_hashes,
        "download_url": str(file.get("downloadUrl") or version.get("downloadUrl") or ""),
        "size_kb": file.get("sizeKB"),
        "trained_words": [str(w) for w in (version.get("trainedWords") or [])],
        "thumb_url": str(thumb or ""),
        "civitai_url": f"https://civitai.com/models/{model.get('id')}",
        "published_at": str(version.get("publishedAt") or ""),
        "tags": [str(t).lower() for t in (model.get("tags") or [])[:30]],
        "nsfw": bool(model.get("nsfw")),
        "stats": {
            "downloads": int(stats.get("downloadCount") or 0),
            "likes": int(stats.get("thumbsUpCount") or 0),
        },
    }


async def _civitai_search_loras(cfg: dict, *, query: str, base_model: str,
                                lora_type: str, sort: str, nsfw_mode: str,
                                cursor: str, limit: int,
                                category: str = "") -> dict:
    import httpx
    if nsfw_mode not in CIVITAI_NSFW_MODES:
        nsfw_mode = "off"
    sort = sort if sort in CIVITAI_SORTS else CIVITAI_SORTS[0]
    # With a `query`, Civitai routes to Meilisearch which ignores `sort` and
    # returns relevance order — so pull several full pages and sort proxy-side.
    pages = CIVITAI_SEARCH_PAGES if query else 1
    fetch_limit = 100 if query else max(1, min(100, limit))
    params = [
        ("types", lora_type if lora_type in CIVITAI_LORA_TYPES else "LORA"),
        ("sort", sort),
        ("limit", str(fetch_limit)),
        ("nsfw", "false" if nsfw_mode == "off" else "true"),
    ]
    if category not in CIVITAI_CATEGORIES:
        category = ""
    if query:
        params.append(("query", query))
    elif category:
        # tag= only works without query; queried searches post-filter below.
        params.append(("tag", category))
    if base_model:
        params.append(("baseModels", base_model))

    raw_items: list[dict] = []
    page_cursor = cursor
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for _ in range(pages):
            page_params = params + ([("cursor", page_cursor)] if page_cursor else [])
            try:
                resp = await client.get(f"{CIVITAI_BASE}/models",
                                        headers=_civitai_headers(cfg), params=page_params)
            except httpx.TransportError as e:
                if raw_items:
                    break  # keep what earlier pages returned
                raise RuntimeError(f"Civitai search failed: {e}")
            if resp.status_code == 401:
                raise RuntimeError("Civitai rejected the request: invalid API key")
            if resp.status_code != 200:
                if raw_items:
                    break
                raise RuntimeError(f"Civitai search failed ({resp.status_code}): "
                                   f"{resp.text[:300]}")
            body = resp.json()
            raw_items.extend(body.get("items") or [])
            page_cursor = str((body.get("metadata") or {}).get("nextCursor") or "")
            if not page_cursor:
                break

    seen_ids: set = set()
    items = []
    for model in raw_items:
        flat = _flatten_civitai_model(model)
        if flat is None or flat["id"] in seen_ids:
            continue
        seen_ids.add(flat["id"])
        items.append(flat)
    if nsfw_mode == "only":
        items = [i for i in items if i["nsfw"]]
    if query and category:
        items = [i for i in items if category in i["tags"]]
    if query:
        sort_keys = {
            "Most Downloaded": lambda i: i["stats"]["downloads"],
            "Highest Rated": lambda i: i["stats"]["likes"],
            "Newest": lambda i: i.get("published_at") or "",
        }
        items.sort(key=sort_keys[sort], reverse=True)
    return {"items": items, "next_cursor": page_cursor}


def _lora_index_path() -> Path:
    return _data_dir() / "novita_lora_index.json"


def _read_lora_index_cache(allow_stale: bool = False) -> dict | None:
    """The cached hash-prefix index, or None when missing/expired/corrupt.
    allow_stale serves an expired index too (good enough for browse badges)."""
    path = _lora_index_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(cached, dict) or not isinstance(cached.get("hashes"), dict):
        return None
    if not allow_stale and \
            time.time() - float(cached.get("fetched_at") or 0) > NOVITA_LORA_INDEX_TTL_S:
        return None
    return cached["hashes"]


async def _novita_lora_index(cfg: dict, force: bool = False) -> dict:
    """SHA256-prefix -> sd_name_in_api for every ready LoRA in Novita's public
    Civitai-mirrored catalog. The hash prefix is the only reliable join key
    (see NOVITA_HASH_PREFIX_LEN comment); the catalog is small enough to sync
    whole and cache on disk."""
    if not force:
        cached = _read_lora_index_cache()
        if cached is not None:
            return cached
    async with _get_lora_index_lock():
        if not force:  # another request may have built it while we waited
            cached = _read_lora_index_cache()
            if cached is not None:
                return cached
        hashes: dict[str, str] = {}
        cursor = ""
        for _ in range(NOVITA_LORA_INDEX_MAX_PAGES):
            body = await _novita_list_models(cfg, "", cursor, 100, types="lora")
            models = body.get("models") or []
            for m in models:
                h = str(m.get("hash_sha256") or "").upper()
                sd_name = str(m.get("sd_name_in_api") or m.get("sd_name") or "")
                # Novita also mirrors Civitai training-data archives as "lora"
                # entries; a .zip is not a loadable weight.
                if len(h) >= NOVITA_HASH_PREFIX_LEN and sd_name \
                        and not sd_name.lower().endswith(".zip") \
                        and m.get("status") == 1:
                    hashes.setdefault(h[:NOVITA_HASH_PREFIX_LEN], sd_name)
            cursor = str((body.get("pagination") or {}).get("next_cursor") or "")
            if not models or not cursor:
                break
        _atomic_write_json(_lora_index_path(),
                           {"fetched_at": time.time(), "hashes": hashes})
        return hashes


def _entry_hashes(entry: dict) -> list[str]:
    """The entry's SHA256s, newest-version-first (all_hashes keeps the source's
    version ordering), deduped."""
    ordered = [str(entry.get("sha256") or "").lower()]
    ordered += [str(h).lower() for h in (entry.get("all_hashes") or []) if h]
    seen: set = set()
    return [h for h in ordered if h and not (h in seen or seen.add(h))]


def _match_hashes(index: dict, entry: dict) -> str | None:
    """First hash-prefix hit in the Novita index, so when Novita mirrors
    several versions the most recent one wins."""
    for h in _entry_hashes(entry):
        sd_name = index.get(h[:NOVITA_HASH_PREFIX_LEN].upper())
        if sd_name:
            return sd_name
    return None


async def _novita_match_lora(cfg: dict, entry: dict,
                             index: dict | None = None) -> dict | None:
    """Find the saved LoRA in Novita's Civitai-mirrored catalog by SHA256
    prefix. Callers doing bulk work should build the index once and pass it
    in."""
    if not _entry_hashes(entry):
        return None
    if index is None:
        index = await _novita_lora_index(cfg)
    sd_name = _match_hashes(index, entry)
    return {"sd_name_in_api": sd_name} if sd_name else None


def _spawn_lora_index_refresh(cfg: dict) -> None:
    """Fire-and-forget index build, so browse endpoints never wait on the
    ~26-page Novita sync. _lora_index_lock dedupes concurrent builds."""
    async def _run():
        try:
            await _novita_lora_index(cfg)
        except RuntimeError as e:
            print(f"[Image Gen] Background LoRA index build failed: {e}")
    task = asyncio.get_running_loop().create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _annotate_novita_availability(cfg: dict, items: list[dict]) -> None:
    """Badge browse results with whether Novita already mirrors them, so the
    user knows availability before saving. Never blocks: a stale index still
    answers (novita_available true/false), no index at all degrades to None
    ("unknown") while a rebuild runs in the background."""
    index = _read_lora_index_cache(allow_stale=True)
    if cfg.get("api_key") and _read_lora_index_cache() is None:
        _spawn_lora_index_refresh(cfg)
    for item in items:
        if _base_family(item.get("base_model")) == "flux":
            continue  # flux rides download URLs; the UI shows "via link"
        if index is None or not _entry_hashes(item):
            item["novita_available"] = None
            continue
        sd_name = _match_hashes(index, item)
        item["novita_available"] = bool(sd_name)
        if sd_name:
            item["novita_sd_name"] = sd_name


async def _validate_novita_key(key: str) -> bool:
    """True/False for accepted/rejected; raises RuntimeError when Novita is
    unreachable (so the caller can distinguish 'bad key' from 'no answer')."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.get(
                f"{NOVITA_BASE}/v3/model",
                headers={"Authorization": f"Bearer {key}", "accept": "application/json"},
                params={"pagination.limit": 1})
    except httpx.TransportError as e:
        raise RuntimeError(f"Could not reach Novita: {e}")
    if resp.status_code in (401, 403):
        return False
    if resp.status_code == 200:
        return True
    raise RuntimeError(f"Novita answered with HTTP {resp.status_code} — try again later")


async def _validate_civitai_key(key: str) -> bool:
    """Civitai's /models ignores bad tokens, but /me 401s on them."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.get(
                f"{CIVITAI_BASE}/me",
                headers={"Authorization": f"Bearer {key}", "accept": "application/json"})
    except httpx.TransportError as e:
        raise RuntimeError(f"Could not reach Civitai: {e}")
    if resp.status_code in (401, 403):
        return False
    if resp.status_code == 200:
        return True
    raise RuntimeError(f"Civitai answered with HTTP {resp.status_code} — try again later")


# --------------------------------------------------------------------------
# Hugging Face Hub client (LoRA browsing)
# --------------------------------------------------------------------------

def _hf_headers(cfg: dict) -> dict:
    headers = {"accept": "application/json"}
    key = str(cfg.get("hf_api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _hf_entry_id(repo_id: str) -> str:
    """Library-entry id for a Hub repo. Slash-free so the /loras/{lora_id}
    path routes work; the prefix keeps it clear of numeric Civitai ids."""
    return "hf:" + repo_id.replace("/", "__")


def _hf_base_model_name(tags: list) -> str:
    """Civitai-style family name from the repo's base_model tags, so all the
    downstream family logic (_base_family, checkpoint matching) reads HF
    entries exactly like Civitai ones."""
    bases = [str(t)[len("base_model:"):].lower() for t in tags or []
             if str(t).startswith("base_model:")]
    for base in bases:
        if "flux.2" in base or "flux-2" in base:
            return "Flux.2 D"
        if "flux" in base:
            return "Flux.1 D"
        if "xl-base" in base or "sdxl" in base:
            return "SDXL 1.0"
        if "stable-diffusion-v1-5" in base or "sd-v1-5" in base or "sd1.5" in base:
            return "SD 1.5"
        if "pony" in base:
            return "Pony"
        if "illustrious" in base:
            return "Illustrious"
        if "noob" in base:
            return "NoobAI"
    # Unknown bases pass through raw: _base_family may still classify them
    # (e.g. a plain "...FLUX..." repo id), otherwise the entry is never
    # usable — same as an unknown Civitai base today.
    return bases[0] if bases else ""


def _hf_pick_safetensors(siblings: list) -> tuple[dict | None, list[str], int]:
    """(primary file, every file's SHA256, safetensors count) for a repo's
    sibling list. Primary = largest by LFS size (LoRA repos almost always have
    exactly one); all hashes are kept so Novita's mirror matches whichever
    file it picked up. Listing responses carry no lfs info — then the first
    file wins and the hash list is empty until the detail fetch."""
    files = [s for s in siblings or []
             if str(s.get("rfilename") or "").lower().endswith(".safetensors")]
    if not files:
        return None, [], 0
    primary = max(files, key=lambda s: (s.get("lfs") or {}).get("size") or 0)
    hashes = []
    for s in files:
        h = str((s.get("lfs") or {}).get("sha256") or "").lower()
        if h and h not in hashes:
            hashes.append(h)
    return primary, hashes, len(files)


def _flatten_hf_model(model: dict) -> dict | None:
    """Reduce a Hub model (listing hit or ?blobs=true detail — same shape,
    details just carry lfs hashes and cardData) to the library-entry shape.
    Returns None for repos without a .safetensors file."""
    repo_id = str(model.get("id") or model.get("modelId") or "")
    if not repo_id:
        return None
    siblings = model.get("siblings") or []
    primary, all_hashes, file_count = _hf_pick_safetensors(siblings)
    if primary is None:
        return None
    filename = str(primary.get("rfilename") or "")
    tags = [str(t) for t in (model.get("tags") or [])]
    owner, _, name = repo_id.rpartition("/")
    card = model.get("cardData") or {}
    trigger = str(card.get("instance_prompt") or "").strip()
    lfs = primary.get("lfs") or {}
    thumb = next(
        (str(s.get("rfilename")) for s in siblings
         if str(s.get("rfilename") or "").lower().endswith(
             (".png", ".jpg", ".jpeg", ".webp"))),
        "")
    return {
        "id": _hf_entry_id(repo_id),
        "model_id": None,
        "source": "hf",
        "repo_id": repo_id,
        "name": name or repo_id,
        "version_name": filename,
        "creator": owner,
        "type": "LORA",
        "base_model": _hf_base_model_name(tags),
        "sha256": str(lfs.get("sha256") or "").lower(),
        "all_hashes": all_hashes,
        "download_url": f"{HF_PAGE_BASE}/{repo_id}/resolve/main/{quote(filename)}",
        "size_kb": (lfs.get("size") or 0) / 1024 or None,
        "trained_words": [trigger] if trigger else [],
        "thumb_url": f"{HF_PAGE_BASE}/{repo_id}/resolve/main/{quote(thumb)}" if thumb else "",
        "civitai_url": "",
        "page_url": f"{HF_PAGE_BASE}/{repo_id}",
        "published_at": str(model.get("lastModified") or ""),
        "tags": [t.lower() for t in tags[:30]],
        "nsfw": HF_NSFW_TAG in tags,
        # Gated/private downloads need an auth header Novita's server-side
        # fetch cannot send, so these can be browsed but not saved.
        "gated": bool(model.get("gated")) or bool(model.get("private")),
        "file_count": file_count,
        "stats": {
            "downloads": int(model.get("downloads") or 0),
            "likes": int(model.get("likes") or 0),
        },
    }


async def _hf_model_detail(client, cfg: dict, repo_id: str) -> dict | None:
    """?blobs=true detail for one repo (adds lfs sha256/size + cardData), None
    on any failure. In-process TTL cache: pagination and re-searches keep
    hitting the same repos."""
    import httpx
    cached = _hf_detail_cache.get(repo_id)
    if cached and time.time() - cached[0] < HF_DETAIL_CACHE_TTL_S:
        return cached[1]
    try:
        resp = await client.get(f"{HF_API_BASE}/models/{repo_id}",
                                headers=_hf_headers(cfg), params={"blobs": "true"})
    except httpx.TransportError:
        return None
    if resp.status_code != 200:
        return None
    detail = resp.json()
    if not isinstance(detail, dict):
        return None
    _hf_detail_cache[repo_id] = (time.time(), detail)
    return detail


async def _hf_enrich_items(cfg: dict, items: list[dict]) -> None:
    """Fill each browse item's hashes/size from its repo detail, concurrently
    and failure-tolerantly — an item that cannot be enriched just keeps its
    listing-level data (no hashes -> availability unknown)."""
    if not items:
        return
    import httpx
    sem = asyncio.Semaphore(HF_DETAIL_CONCURRENCY)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        async def enrich(item: dict) -> None:
            async with sem:
                detail = await _hf_model_detail(client, cfg, item["repo_id"])
            if detail:
                flat = _flatten_hf_model(detail)
                if flat:
                    item.update(flat)

        await asyncio.gather(*(enrich(i) for i in items))


async def _hf_search_loras(cfg: dict, *, query: str, base_model: str,
                           sort: str, nsfw_mode: str, cursor: str,
                           limit: int) -> dict:
    import httpx
    if nsfw_mode not in CIVITAI_NSFW_MODES:
        nsfw_mode = "off"
    sort = sort if sort in HF_SORTS else HF_SORTS[0]
    if cursor:
        # The Hub paginates via the Link response header, so the cursor is a
        # full URL. Only follow it back to the Hub itself (SSRF guard).
        if not cursor.startswith(f"{HF_API_BASE}/models"):
            raise RuntimeError("Bad pagination cursor")
        url: str = cursor
        params = None
    else:
        url = f"{HF_API_BASE}/models"
        params = [("filter", "lora"),
                  ("sort", HF_SORT_PARAMS[sort]),
                  ("direction", "-1"),
                  ("limit", str(max(1, min(100, limit)))),
                  ("full", "true"),
                  ("cardData", "true")]
        if query:
            params.append(("search", query))
        if base_model in HF_BASE_MODELS:
            params.append(("filter", HF_BASE_MODELS[base_model]))

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        try:
            resp = await client.get(url, headers=_hf_headers(cfg), params=params)
        except httpx.TransportError as e:
            raise RuntimeError(f"Hugging Face search failed: {e}")
    if resp.status_code == 401:
        raise RuntimeError("Hugging Face rejected the request: invalid token")
    if resp.status_code != 200:
        raise RuntimeError(f"Hugging Face search failed ({resp.status_code}): "
                           f"{resp.text[:300]}")
    body = resp.json()
    next_cursor = str((resp.links.get("next") or {}).get("url") or "")

    items = []
    for model in body if isinstance(body, list) else []:
        flat = _flatten_hf_model(model)
        if flat is None:
            continue
        if nsfw_mode == "off" and flat["nsfw"]:
            continue
        if nsfw_mode == "only" and not flat["nsfw"]:
            continue
        items.append(flat)
    return {"items": items, "next_cursor": next_cursor}


async def _hf_refresh_entry_hashes(cfg: dict, entry: dict) -> None:
    """Second chance for an HF entry saved without hashes (browse-time
    enrichment can fail): one detail fetch before Novita matching."""
    import httpx
    repo_id = str(entry.get("repo_id") or "")
    if not repo_id:
        return
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        detail = await _hf_model_detail(client, cfg, repo_id)
    if not detail:
        return
    flat = _flatten_hf_model(detail)
    if flat:
        for k in ("sha256", "all_hashes", "download_url", "size_kb", "version_name"):
            entry[k] = flat[k]


async def _validate_hf_key(key: str) -> bool:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.get(
                f"{HF_API_BASE}/whoami-v2",
                headers={"Authorization": f"Bearer {key}", "accept": "application/json"})
    except httpx.TransportError as e:
        raise RuntimeError(f"Could not reach Hugging Face: {e}")
    if resp.status_code in (401, 403):
        return False
    if resp.status_code == 200:
        return True
    raise RuntimeError(f"Hugging Face answered with HTTP {resp.status_code} — try again later")


def _normalize_lora_entry(item: dict) -> dict:
    """Library entry from a browser selection, with activation defaults."""
    entry = {k: item.get(k) for k in (
        "id", "model_id", "name", "version_name", "creator", "type", "base_model",
        "sha256", "all_hashes", "download_url", "size_kb", "trained_words",
        "thumb_url", "civitai_url", "nsfw", "stats", "source", "repo_id",
        "page_url")}
    entry["id"] = str(entry.get("id") or "")
    # Entries saved before multi-source support have no source field.
    entry["source"] = str(entry.get("source") or "civitai")
    entry["repo_id"] = str(entry.get("repo_id") or "")
    entry["page_url"] = str(entry.get("page_url") or "")
    entry["all_hashes"] = [str(h).lower() for h in (entry.get("all_hashes") or [])][:10]
    entry["trained_words"] = [str(w) for w in (entry.get("trained_words") or [])][:20]
    entry.update({
        "saved_at": _now(),
        "active": False,
        "strength": LORA_DEFAULT_WEIGHT,
        "sd_name_override": "",
        "condition": "",     # condition / weight instructions for the AI modes
        "llm_mode": "off",   # what the per-image LLM decides; see LORA_LLM_MODES
        "novita": None,
        "novita_checked_at": None,
    })
    return entry


# --------------------------------------------------------------------------
# Background pipeline
# --------------------------------------------------------------------------

def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "story"))[:40].strip("-") or "story"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hook_sdk(sdk):
    """Router-initiated runs have no hook sdk; fall back to the engine's."""
    if sdk is not None:
        return sdk
    engine = _services.get("engine")
    return getattr(engine, "sdk", None)


def _character_descriptor(race, gender, appearance) -> str:
    ident = " ".join(p for p in (str(gender or "").strip(), str(race or "").strip()) if p)
    look = str(appearance or "").strip()
    return "; ".join(p for p in (ident, look) if p)


def _scene_presence_ids(state: dict) -> set[str] | None:
    """The NPC system's published scene roster (who is physically present),
    or None when it is absent or stale (module disabled, older saves) --
    callers then fall back to the recency heuristic."""
    presence = state.get("module_data", {}).get("wb_npc_system", {}).get("scene_presence")
    if not isinstance(presence, dict):
        return None
    try:
        if abs(int(state.get("turn") or 0) - int(presence.get("turn"))) > 1:
            return None
    except (TypeError, ValueError):
        return None
    return {str(i) for i in presence.get("npc_ids") or []}


def _named_in(npc: dict, text: str) -> bool:
    name = str(npc.get("name") or "").strip()
    return bool(name) and bool(re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE))


def _presence_pinned(npc: dict, turn) -> bool:
    """Player-authority pin the NPC system stamps when a character is manually
    added or activated from the browser -- honored while fresh, so a presence
    roster computed before the character existed cannot hide them."""
    pinned = npc.get("presence_pinned_turn")
    try:
        return pinned is not None and int(turn or 0) - int(pinned) <= 1
    except (TypeError, ValueError):
        return False


def _character_snapshot(state: dict) -> dict | None:
    """Canonical appearances for the prompt writer and the LoRA gate, from
    whichever character modules happen to be active (both optional): the
    player tracker's characters["default_player"] and the NPC system's
    introduced bank. NPCs are the ones the NPC system judged present in the
    scene (plus any named in the latest narration or freshly pinned by a
    manual add/activation, which the roster -- computed before those
    happened -- cannot know about); without a fresh roster,
    recently-interacted NPCs stand in. The snapshot is
    uncapped: the prompt writer and the LoRA gate both need everyone
    present. Returns None when neither module has anything to say."""
    player_out = None
    player = state.get("characters", {}).get("default_player") or {}
    appearance = player.get("short_appearance") or player.get("full_appearance") or ""
    if str(appearance).strip():
        player_out = {
            "key": "player",
            "name": str(player.get("name") or "").strip() or "the player character",
            "descriptor": _character_descriptor(player.get("race"), player.get("gender"), appearance),
        }

    bank = state.get("module_data", {}).get("wb_npc_system", {}).get("characters", {})
    # An "active" status counts as known even when the introduced flag is
    # stale (records activated from the browser before the flags were synced).
    known = [n for n in bank.values()
             if isinstance(n, dict)
             and (n.get("introduced") or n.get("status") == "active")
             and str(n.get("appearance") or "").strip()
             and n.get("status") not in ("dead", "deceased", "departed")]
    known.sort(key=lambda n: (not n.get("traveling_with_player"),
                              -int(n.get("last_interaction_turn") or 0)))
    candidates = known
    presence = _scene_presence_ids(state)
    if presence is not None:
        latest = str((state.get("history") or [""])[-1])
        candidates = [n for n in known
                      if str(n.get("id")) in presence or _named_in(n, latest)
                      or _presence_pinned(n, state.get("turn"))]

    def _sheet(npc):
        descriptor = _character_descriptor(npc.get("race"), npc.get("gender"), npc.get("appearance"))
        name = str(npc.get("name") or "").strip() or "Unknown"
        key = str(npc.get("id") or "") or f"name:{name.lower()}"
        return {"key": key, "name": name, "descriptor": descriptor}

    # One sheet object per NPC, shared by both lists, so attached tags are
    # visible wherever the character appears.
    sheets = {id(n): _sheet(n) for n in known}
    npcs = [sheets[id(n)] for n in candidates]
    all_npcs = [sheets[id(n)] for n in known]

    if not player_out and not all_npcs:
        return None
    # npcs: who is in the scene (feeds the image prompt); all_npcs: every
    # known living character (feeds the LoRA gate, whose per-character
    # conditions must be able to match anyone regardless of scene presence).
    out = {"player": player_out, "npcs": npcs, "all_npcs": all_npcs}
    _attach_cached_tags(state.get("active_save_id") or "unknown", out)
    return out


def _attach_cached_tags(save_id: str, characters: dict) -> None:
    """Copy each character's cached appearance tags onto their sheet -- but
    only when the cached hash still matches the descriptor, so an edited
    appearance instantly falls back to the description instead of serving
    stale tags while the regeneration is in flight."""
    entries = _read_tag_cache().get(str(save_id))
    if not isinstance(entries, dict) or not entries:
        return
    sheets = list(characters.get("all_npcs") or [])
    if characters.get("player"):
        sheets.append(characters["player"])
    for sheet in sheets:
        entry = entries.get(sheet.get("key"))
        if isinstance(entry, dict) and str(entry.get("tags") or "").strip() \
                and entry.get("hash") == _appearance_hash(sheet["descriptor"]):
            sheet["tags"] = str(entry["tags"])


def _snapshot_names(characters: dict | None) -> list[str]:
    if not characters:
        return []
    names = [characters["player"]["name"]] if characters.get("player") else []
    return names + [n["name"] for n in characters.get("npcs") or []]


def _tag_entry_stale(save_id: str, sheet: dict) -> bool:
    """Whether a sheet's cached tags are missing or keyed to an outdated
    descriptor (regeneration needed). Reads the cache fresh so mid-pipeline
    manual edits are seen."""
    entry = (_read_tag_cache().get(str(save_id)) or {}).get(sheet.get("key"))
    return not (isinstance(entry, dict)
                and entry.get("hash") == _appearance_hash(sheet["descriptor"]))


def _characters_needing_tags(save_id: str, characters: dict) -> list[dict]:
    """Sheets whose cached appearance tags are missing or hash-stale. Drawn
    from all_npcs (every known living character), not just the scene roster,
    so tags are ready before a character first enters frame."""
    entries = _read_tag_cache().get(str(save_id))
    if not isinstance(entries, dict):
        entries = {}
    sheets = list(characters.get("all_npcs") or [])
    if characters.get("player"):
        sheets.append(characters["player"])
    needing = []
    for sheet in sheets:
        entry = entries.get(sheet.get("key"))
        if not (isinstance(entry, dict)
                and entry.get("hash") == _appearance_hash(sheet["descriptor"])):
            needing.append(sheet)
    return needing


def _spawn_tag_backfill(save_id: str, characters: dict | None, sdk) -> None:
    """Fire-and-forget precomputation of per-character appearance tags for
    tag-style checkpoints. Never blocks or fails the caller; characters whose
    tags aren't ready yet simply fall back to their description. Deliberately
    not gated on cfg["enabled"]: on_librarian gates that itself, and manual
    /image or /generate runs should warm the cache even when auto
    illustration is off."""
    if not characters:
        return
    cfg = _load_config()
    if not cfg.get("api_key") or not cfg.get("model_name"):
        return
    if _prompt_style(cfg) != "tags" or not cfg.get("character_reference_enabled", True):
        return
    if _get_tag_lock().locked():
        return
    worklist = _characters_needing_tags(save_id, characters)
    if not worklist:
        return
    roster_keys = {s.get("key") for s in characters.get("all_npcs") or []}
    roster_keys.add("player")
    task = asyncio.get_running_loop().create_task(
        _tag_backfill_pipeline(save_id, worklist, roster_keys, cfg, _hook_sdk(sdk)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _tag_backfill_pipeline(save_id: str, worklist: list[dict],
                                 roster_keys: set, cfg: dict, sdk) -> None:
    """Generate and cache appearance tags for each sheet, sequentially (one
    LLM call in flight at a time -- adopting a story with many NPCs must not
    fan out) and capped per run; the next turn's backfill resumes the rest.
    Results are persisted incrementally, so a restart mid-run loses nothing
    already finished. Failures only log: a missing entry retries next turn."""
    try:
        async with _get_tag_lock():
            if sdk is None:
                return
            for sheet in worklist[:TAG_BACKFILL_MAX_PER_RUN]:
                # A manual edit (character-tags endpoint) may have landed since
                # this worklist was computed; a fresh entry needs no LLM call.
                if not _tag_entry_stale(save_id, sheet):
                    continue
                prompt = (CHARACTER_TAG_PROMPT
                          .replace("{name}", sheet["name"])
                          .replace("{descriptor}", sheet["descriptor"]))
                try:
                    sdk.llm._current_module = MODULE_ID
                    raw = await sdk.llm.generate(
                        prompt,
                        model_preference=cfg.get("prompt_model_preference", "smartest"))
                except Exception as e:
                    print(f"[Image Gen] Tag generation failed for {sheet['name']}: {e}")
                    continue
                finally:
                    sdk.llm._current_module = ""
                tags = _clean_character_tags(raw, cfg)
                if not tags:
                    print(f"[Image Gen] Unusable tag reply for {sheet['name']} "
                          f"(will retry): {raw[:200]!r}")
                    continue
                # This block is synchronous (no awaits), so in the engine's
                # single event loop the read-modify-write cannot interleave
                # with the character-tags endpoints' own synchronous RMW.
                if not _tag_entry_stale(save_id, sheet):
                    # A manual edit for the current descriptor landed while
                    # the LLM call was in flight -- the player wins.
                    continue
                cache = _read_tag_cache()
                entries = cache.get(str(save_id))
                if not isinstance(entries, dict):
                    entries = {}
                entries[sheet["key"]] = {
                    "hash": _appearance_hash(sheet["descriptor"]),
                    "tags": tags,
                    "name": sheet["name"],
                    "updated_at": _now(),
                }
                # Entries whose character left the roster (dead, departed,
                # renamed) are evicted whenever this save's section is written.
                cache[str(save_id)] = {k: v for k, v in entries.items()
                                       if k in roster_keys}
                _write_tag_cache(cache)
    except Exception as e:
        print(f"[Image Gen] Tag backfill failed: {e}")


def _spawn_generation(*, save_id: str, turn: int, narration: str, history: str,
                      sdk, trigger: str = "auto", prompt_override: str | None = None,
                      characters: dict | None = None) -> str | None:
    """Create a pending record and fire the pipeline task. Returns the record id,
    or None when a generation is already running (caller decides what that means)."""
    lock = _get_gen_lock()
    if lock.locked():
        return None

    cfg = _load_config()
    record_id = f"{_slug(save_id)}_{int(turn or 0)}_{uuid.uuid4().hex[:8]}"
    # Stamped on the record so the UIs can show one in-progress placeholder
    # per expected image while the batch generates.
    image_num = max(1, min(IMAGE_NUM_MAX, int(cfg.get("image_num", 1) or 1)))
    record = {
        "id": record_id,
        "save_id": save_id,
        "turn": int(turn or 0),
        "status": "pending",
        "trigger": trigger,
        "filename": None,
        "filenames": [],
        "image_prompt": prompt_override or "",
        "image_prompts": [],
        "narration_excerpt": (narration or "")[:200],
        "model_name": cfg.get("model_name", ""),
        "loras": _applied_lora_names(cfg),
        "characters": _snapshot_names(characters),
        "width": cfg.get("width"),
        "height": cfg.get("height"),
        "image_num": image_num,
        "error": None,
        "created_at": _now(),
        "completed_at": None,
        "duration_s": None,
    }

    async def _run():
        await _append_record(record)
        await _generation_pipeline(record_id, cfg, narration, history,
                                   _hook_sdk(sdk), prompt_override, characters)

    task = asyncio.get_running_loop().create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return record_id


async def _retry_step(step_name: str, fn, retries: int):
    """Run one pipeline step, re-running it up to `retries` extra times after
    a failure (with a short growing delay between attempts). NonRetryableError
    aborts immediately — the same input would fail the same way again."""
    attempts = max(0, min(STEP_RETRIES_MAX, int(retries or 0))) + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except NonRetryableError:
            raise
        except Exception as e:
            last_error = e
            if attempt < attempts:
                print(f"[Image Gen] {step_name} failed (attempt {attempt}/{attempts}), "
                      f"retrying: {e}")
                await asyncio.sleep(STEP_RETRY_BASE_DELAY_S * attempt)
    if attempts > 1:
        raise RuntimeError(
            f"{step_name} failed after {attempts} attempts: {last_error}") from last_error
    raise last_error


async def _generation_pipeline(record_id: str, cfg: dict, narration: str,
                               history: str, sdk, prompt_override: str | None,
                               characters: dict | None = None) -> None:
    started = time.monotonic()
    lock = _get_gen_lock()
    retries = cfg.get("step_retries", STEP_RETRIES_DEFAULT)
    try:
        async with lock:
            # Conditional LoRAs are gated first so both the trigger words fed
            # to the prompt writer and the submit payload see the final set.
            # (No retry here — the gate already fails open on any error.)
            gated = await _apply_lora_conditions(cfg, narration, sdk, characters)
            if gated is not cfg:
                cfg = gated
                await _patch_record(record_id, loras=_applied_lora_names(cfg))

            image_num = max(1, min(IMAGE_NUM_MAX, int(cfg.get("image_num", 1) or 1)))

            if prompt_override:
                # A verbatim prompt (retry, unrefined studio text) cannot be
                # re-varied; the batch shares it and differs by seed only.
                prompts = [_clean_image_prompt(prompt_override)] * image_num
            else:
                if sdk is None:
                    raise RuntimeError("no LLM available to write the image prompt")
                await _patch_record(record_id, status="prompting")

                # One writer call per image, run concurrently: separate calls
                # (plus a per-slot variation hint) give each image its own
                # take on the scene instead of N seeds of one prompt. Each
                # slot counts its own attempts so a refusal's retries carry
                # the fiction reminder while first attempts stay unchanged.
                def _writer(hint):
                    attempts = {"n": 0}

                    async def call():
                        attempts["n"] += 1
                        return await _write_image_prompt(
                            cfg, narration, history, sdk, characters,
                            variation_hint=hint, insist=attempts["n"] > 1)
                    return call

                prompt_results = await asyncio.gather(
                    *(_retry_step("prompt writing",
                                  _writer(_variation_hint(slot, image_num)),
                                  retries)
                      for slot in range(image_num)),
                    return_exceptions=True)
                good = [p for p in prompt_results if not isinstance(p, BaseException)]
                if not good:
                    raise prompt_results[0]
                # A failed writer slot borrows a sibling's prompt (seed still
                # varies) rather than sinking its image or the whole batch.
                prompts = [p if not isinstance(p, BaseException) else good[0]
                           for p in prompt_results]

            await _patch_record(record_id, status="generating",
                                image_prompt=prompts[0], image_prompts=prompts)

            # One retryable unit: a failed task cannot be re-polled and the
            # presigned result URL expires, so any failure past submission
            # recovers by resubmitting a fresh task. Each slot's prompt lives
            # in a mutable cell so a content-filter refusal can swap in an
            # LLM-softened rewrite before the next step retry resubmits.
            cells = [{"prompt": p} for p in prompts]

            async def _generate_once(cell):
                try:
                    task_id = await _novita_submit(cfg, cell["prompt"])
                    image_url = await _novita_poll(cfg, task_id)
                    return await _download(image_url)
                except ProviderRefusal as e:
                    # Verbatim user text is never rewritten behind the user's
                    # back; only LLM-written prompts get softened.
                    if prompt_override is not None or sdk is None:
                        raise
                    cell["prompt"] = await _soften_image_prompt(
                        cfg, cell["prompt"], str(e), sdk)
                    raise RuntimeError(
                        f"retrying with a softened prompt after refusal: {e}")

            results = await asyncio.gather(
                *(_retry_step("image generation",
                              lambda c=cell: _generate_once(c), retries)
                  for cell in cells),
                return_exceptions=True)

            filenames = []
            kept_prompts = []
            for slot, result in enumerate(results):
                if isinstance(result, BaseException):
                    continue
                data, ext = result
                # Slot 0 keeps the pre-batch single-image name, so one-image
                # records look exactly like they always did.
                filename = f"{record_id}.{ext}" if slot == 0 else f"{record_id}_{slot}.{ext}"
                path = _data_dir() / "images" / filename
                tmp = path.with_suffix(path.suffix + ".tmp")
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                filenames.append(filename)
                # The cell holds what was actually submitted (softened or not).
                kept_prompts.append(cells[slot]["prompt"])

            failures = [r for r in results if isinstance(r, BaseException)]
            if not filenames:
                raise failures[0]
            if failures:
                # Part of the batch made it: keep the survivors rather than
                # failing the whole record over a missing sibling image.
                print(f"[Image Gen] {record_id}: {len(failures)}/{image_num} "
                      f"parallel images failed: {failures[0]}")

            # image_prompts stays aligned with filenames, so the viewer can
            # caption each image with the prompt that produced it.
            await _patch_record(
                record_id, status="done", filename=filenames[0], filenames=filenames,
                image_prompt=kept_prompts[0], image_prompts=kept_prompts,
                completed_at=_now(), duration_s=round(time.monotonic() - started, 1))
            print(f"[Image Gen] {record_id} done in {round(time.monotonic() - started, 1)}s")
    except Exception as e:
        # A failed illustration must never surface as an exception anywhere —
        # it only marks its own record.
        print(f"[Image Gen] {record_id} failed: {e}")
        try:
            await _patch_record(
                record_id, status="error", error=str(e)[:500],
                completed_at=_now(), duration_s=round(time.monotonic() - started, 1))
        except Exception as patch_err:
            print(f"[Image Gen] Could not record failure for {record_id}: {patch_err}")


# --------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------

def _own_data(state: dict) -> dict:
    return state.get("module_data", {}).get(MODULE_ID, {})


async def on_gather_context(state: dict, sdk) -> dict:
    if not _own_data(state):
        return {"module_data": {MODULE_ID: {"turns_since_image": 0}}}
    return {}


async def on_librarian(state: dict, sdk) -> dict | None:
    cfg = _load_config()
    if not cfg.get("enabled") or not cfg.get("api_key") or not cfg.get("model_name"):
        return None
    history = state.get("history", [])
    if not history:
        return None

    count = int(_own_data(state).get("turns_since_image", 0) or 0) + 1
    interval = max(1, int(cfg.get("interval", 3) or 3))

    # Every enabled turn -- not just image turns -- so a character's tags are
    # usually ready by the time they first appear in an illustration.
    characters = _character_snapshot(state)
    _spawn_tag_backfill(state.get("active_save_id") or "unknown", characters, sdk)

    if count >= interval:
        record_id = _spawn_generation(
            save_id=state.get("active_save_id") or "unknown",
            turn=state.get("turn", 0),
            narration=str(history[-1]),
            history="\n".join(str(h) for h in history[-6:-1]),
            sdk=sdk,
            characters=characters,
        )
        if record_id:
            print(f"[Image Gen] Turn {state.get('turn')}: auto illustration started ({record_id}).")
            return {"module_data": {MODULE_ID: {"turns_since_image": 0, "last_trigger": record_id}}}
        # Busy: keep the ripe counter so the next turn retries.

    return {"module_data": {MODULE_ID: {"turns_since_image": count}}}


def _latest_narration(state: dict) -> str:
    for message in reversed(state.get("chat_messages", [])):
        if message.get("role") in ("ai", "assistant") and str(message.get("content", "")).strip():
            return str(message["content"])
    history = state.get("history", [])
    return str(history[-1]) if history else ""


async def on_command_image(args: list[str], state: dict, sdk) -> dict:
    cfg = _load_config()
    if not cfg.get("api_key"):
        return {"message": "[Image Gen] No API key configured. Add one in Image Studio (main menu).",
                "signal": "end_turn"}
    if not cfg.get("model_name"):
        return {"message": "[Image Gen] No model selected. Pick one in Image Studio (main menu).",
                "signal": "end_turn"}

    narration = _latest_narration(state)
    if not narration:
        return {"message": "[Image Gen] Nothing to illustrate yet — play a turn first.",
                "signal": "end_turn"}

    history = state.get("history", [])
    characters = _character_snapshot(state)
    _spawn_tag_backfill(state.get("active_save_id") or "unknown", characters, sdk)
    record_id = _spawn_generation(
        save_id=state.get("active_save_id") or "unknown",
        turn=state.get("turn", 0),
        narration=narration,
        history="\n".join(str(h) for h in history[-6:-1]),
        sdk=sdk,
        trigger="manual",
        characters=characters,
    )
    if record_id is None:
        return {"message": "[Image Gen] An image is already being generated — give it a moment.",
                "signal": "end_turn"}
    return {
        "message": "[Image Gen] Illustrating the current scene — the image will appear under the latest message shortly.",
        "signal": "end_turn",
        "module_data": {MODULE_ID: {"last_trigger": record_id}},
    }


# --------------------------------------------------------------------------
# Router (mounted at /api/modules/wb_image_gen)
# --------------------------------------------------------------------------

_FILENAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,120}\.(jpg|jpeg|png|webp)")


def _record_files(record: dict) -> list[str]:
    """Every image filename a record owns: multi-image records carry
    `filenames`, records from before the batch feature only `filename`."""
    names = [n for n in (record.get("filenames") or []) if isinstance(n, str)]
    single = record.get("filename")
    if isinstance(single, str) and single and single not in names:
        names.insert(0, single)
    return [n for n in names if _FILENAME_RE.fullmatch(n)]


def _delete_record_files(record: dict) -> None:
    for filename in _record_files(record):
        try:
            (_data_dir() / "images" / filename).unlink(missing_ok=True)
        except OSError as e:
            print(f"[Image Gen] Could not delete {filename}: {e}")


def get_router():
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse, RedirectResponse
    from pydantic import BaseModel

    router = APIRouter()

    class ConfigUpdate(BaseModel):
        enabled: bool | None = None
        api_key: str | None = None
        model_name: str | None = None
        model_base: str | None = None
        width: int | None = None
        height: int | None = None
        image_num: int | None = None
        steps: int | None = None
        guidance_scale: float | None = None
        sampler_name: str | None = None
        negative_prompt: str | None = None
        interval: int | None = None
        step_retries: int | None = None
        prompt_model_preference: str | None = None
        prompt_template: str | None = None
        prompt_template_tags: str | None = None
        pony_quality_tags: str | None = None
        booru_subject_mode: str | None = None
        booru_break_separator: bool | None = None
        prompt_style_mode: str | None = None
        tag_usage_filter: str | None = None
        tag_usage_min_count: int | None = None
        booru_single_subject: bool | None = None   # deprecated alias for booru_subject_mode
        style_suffix: str | None = None
        character_reference_enabled: bool | None = None
        player_in_images: str | None = None
        chat_image_conceal: str | None = None
        civitai_api_key: str | None = None
        civitai_nsfw: str | None = None
        hf_api_key: str | None = None

    class GenerateRequest(BaseModel):
        prompt_override: str | None = None
        save_id: str | None = None
        retry_record_id: str | None = None
        # Treat prompt_override as a scene and run it through the
        # prompt-writer LLM instead of sending it to Novita verbatim.
        refine: bool = False

    class LoraSave(BaseModel):
        id: str
        model_id: int | None = None
        name: str
        version_name: str = ""
        creator: str = ""
        type: str = "LORA"
        base_model: str = ""
        sha256: str = ""
        all_hashes: list[str] = []
        download_url: str = ""
        size_kb: float | None = None
        trained_words: list[str] = []
        thumb_url: str = ""
        civitai_url: str = ""
        nsfw: bool = False
        stats: dict = {}
        source: str = "civitai"
        repo_id: str = ""
        page_url: str = ""
        gated: bool = False

    class LoraPatch(BaseModel):
        active: bool | None = None
        strength: float | None = None
        sd_name_override: str | None = None
        condition: str | None = None
        llm_mode: str | None = None
        trained_words: list[str] | None = None

    class KeySubmit(BaseModel):
        api_key: str

    # Identity fields mirror what _character_snapshot feeds the tag pipeline,
    # so the hash computed here matches the one the backfill would compute.
    class TagCharacter(BaseModel):
        key: str
        name: str = ""
        race: str = ""
        gender: str = ""
        appearance: str = ""

    class TagLookup(BaseModel):
        save_id: str
        characters: list[TagCharacter] = []

    class TagUpdate(BaseModel):
        tags: str
        name: str = ""
        race: str = ""
        gender: str = ""
        appearance: str = ""

    class ProfileCreate(BaseModel):
        name: str
        duplicate_from: str | None = None

    class ProfileRename(BaseModel):
        name: str

    def _public_config(cfg: dict) -> dict:
        out = dict(cfg)
        out["api_key"] = _mask_key(cfg.get("api_key", ""))
        out["has_key"] = bool(cfg.get("api_key"))
        out["civitai_api_key"] = _mask_key(cfg.get("civitai_api_key", ""))
        out["has_civitai_key"] = bool(cfg.get("civitai_api_key"))
        out["hf_api_key"] = _mask_key(cfg.get("hf_api_key", ""))
        out["has_hf_key"] = bool(cfg.get("hf_api_key"))
        out["samplers"] = SAMPLERS
        out["default_prompt_template"] = DEFAULT_PROMPT_TEMPLATE
        out["default_prompt_template_tags"] = DEFAULT_PROMPT_TEMPLATE_TAGS
        out["default_pony_quality_tags"] = DEFAULT_PONY_QUALITY_TAGS
        out["prompt_style"] = _prompt_style(cfg)   # resolved; the stored mode rides in as prompt_style_mode
        out["prompt_style_modes"] = PROMPT_STYLE_MODES
        out["civitai_sorts"] = CIVITAI_SORTS
        out["civitai_lora_types"] = CIVITAI_LORA_TYPES
        out["civitai_nsfw_modes"] = CIVITAI_NSFW_MODES
        out["booru_subject_modes"] = BOORU_SUBJECT_MODES
        out["tag_usage_filter_modes"] = TAG_USAGE_FILTER_MODES
        out["civitai_categories"] = CIVITAI_CATEGORIES
        out["civitai_base_models"] = CIVITAI_BASE_MODELS
        out["hf_sorts"] = HF_SORTS
        out["hf_base_models"] = list(HF_BASE_MODELS)
        out["flux2_model_name"] = FLUX2_MODEL_NAME
        out["checkpoint_family"] = _checkpoint_family(cfg)
        out["lora_weight_min"] = LORA_WEIGHT_MIN
        out["lora_weight_max"] = LORA_WEIGHT_MAX
        out["step_retries_max"] = STEP_RETRIES_MAX
        out["image_num_max"] = IMAGE_NUM_MAX
        store = _load_store()
        out["active_profile"] = cfg.get("active_profile") or store["active_profile"]
        out["profiles"] = [{"id": pid, "name": p.get("name") or pid}
                           for pid, p in store["profiles"].items()]
        return out

    @router.get("/config")
    async def get_config():
        return _public_config(_load_config())

    @router.put("/config")
    async def put_config(update: ConfigUpdate):
        cfg = _load_config()
        incoming = update.model_dump(exclude_none=True)

        key = incoming.pop("api_key", None)
        if key is not None and not key.startswith(KEY_MASK_PREFIX):
            cfg["api_key"] = key.strip()
        civitai_key = incoming.pop("civitai_api_key", None)
        if civitai_key is not None and not civitai_key.startswith(KEY_MASK_PREFIX):
            cfg["civitai_api_key"] = civitai_key.strip()
        hf_key = incoming.pop("hf_api_key", None)
        if hf_key is not None and not hf_key.startswith(KEY_MASK_PREFIX):
            cfg["hf_api_key"] = hf_key.strip()

        if "sampler_name" in incoming and incoming["sampler_name"] not in SAMPLERS:
            raise HTTPException(status_code=400, detail=f"Unknown sampler. Allowed: {SAMPLERS}")
        for side in ("width", "height"):
            if side in incoming:
                incoming[side] = max(128, min(2048, (int(incoming[side]) // 8) * 8))
        if "image_num" in incoming:
            incoming["image_num"] = max(1, min(IMAGE_NUM_MAX, int(incoming["image_num"])))
        if "steps" in incoming:
            incoming["steps"] = max(1, min(100, int(incoming["steps"])))
        if "guidance_scale" in incoming:
            incoming["guidance_scale"] = max(1.0, min(30.0, float(incoming["guidance_scale"])))
        if "interval" in incoming:
            incoming["interval"] = max(1, min(50, int(incoming["interval"])))
        if "step_retries" in incoming:
            incoming["step_retries"] = max(0, min(STEP_RETRIES_MAX,
                                                  int(incoming["step_retries"])))
        if ("prompt_model_preference" in incoming
                and incoming["prompt_model_preference"] not in ("fastest", "balanced", "smartest")):
            raise HTTPException(status_code=400, detail="prompt_model_preference must be a model slot")
        if "civitai_nsfw" in incoming and incoming["civitai_nsfw"] not in CIVITAI_NSFW_MODES:
            raise HTTPException(status_code=400,
                                detail=f"civitai_nsfw must be one of {CIVITAI_NSFW_MODES}")
        if ("player_in_images" in incoming
                and incoming["player_in_images"] not in PLAYER_IN_IMAGES_MODES):
            raise HTTPException(status_code=400,
                                detail=f"player_in_images must be one of {PLAYER_IN_IMAGES_MODES}")
        if ("chat_image_conceal" in incoming
                and incoming["chat_image_conceal"] not in CHAT_IMAGE_CONCEAL_MODES):
            raise HTTPException(status_code=400,
                                detail=f"chat_image_conceal must be one of {CHAT_IMAGE_CONCEAL_MODES}")
        # Stale UIs still send the pre-mode boolean; honor it unless an
        # explicit mode arrives alongside.
        legacy_single = incoming.pop("booru_single_subject", None)
        if legacy_single is not None and "booru_subject_mode" not in incoming:
            incoming["booru_subject_mode"] = "single" if legacy_single else "multi"
        if ("booru_subject_mode" in incoming
                and incoming["booru_subject_mode"] not in BOORU_SUBJECT_MODES):
            raise HTTPException(status_code=400,
                                detail=f"booru_subject_mode must be one of {BOORU_SUBJECT_MODES}")
        if ("prompt_style_mode" in incoming
                and incoming["prompt_style_mode"] not in PROMPT_STYLE_MODES):
            raise HTTPException(status_code=400,
                                detail=f"prompt_style_mode must be one of {PROMPT_STYLE_MODES}")
        if ("tag_usage_filter" in incoming
                and incoming["tag_usage_filter"] not in TAG_USAGE_FILTER_MODES):
            raise HTTPException(status_code=400,
                                detail=f"tag_usage_filter must be one of {TAG_USAGE_FILTER_MODES}")
        if "tag_usage_min_count" in incoming:
            incoming["tag_usage_min_count"] = max(0, min(10_000_000,
                                                         int(incoming["tag_usage_min_count"])))

        cfg.update(incoming)
        _save_config(cfg)
        return _public_config(cfg)

    def _valid_profile_name(store: dict, name: str, exclude: str | None = None) -> str:
        name = str(name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Profile name can't be empty")
        if len(name) > PROFILE_NAME_MAX:
            raise HTTPException(status_code=400,
                                detail=f"Profile name is too long (max {PROFILE_NAME_MAX} chars)")
        for pid, profile in store["profiles"].items():
            if pid != exclude and str(profile.get("name") or "").lower() == name.lower():
                raise HTTPException(status_code=409,
                                    detail=f"A profile named {name!r} already exists")
        return name

    @router.post("/profiles")
    async def create_profile(body: ProfileCreate):
        store = _load_store()
        name = _valid_profile_name(store, body.name)
        if len(store["profiles"]) >= PROFILES_MAX:
            raise HTTPException(status_code=400,
                                detail=f"Profile limit reached ({PROFILES_MAX})")
        if body.duplicate_from is not None:
            source = store["profiles"].get(body.duplicate_from)
            if source is None:
                raise HTTPException(status_code=404, detail="Profile to duplicate not found")
            profile = json.loads(json.dumps(source))
            profile["name"] = name
        else:
            profile = _default_profile(name)
        pid = uuid.uuid4().hex[:8]
        store["profiles"][pid] = profile
        store["active_profile"] = pid  # create-and-configure flow
        _save_store(store)
        return _public_config(_load_config())

    @router.post("/profiles/{pid}/activate")
    async def activate_profile(pid: str):
        store = _load_store()
        if pid not in store["profiles"]:
            raise HTTPException(status_code=404, detail="Profile not found")
        store["active_profile"] = pid
        _save_store(store)
        return _public_config(_load_config())

    @router.patch("/profiles/{pid}")
    async def rename_profile(pid: str, body: ProfileRename):
        store = _load_store()
        if pid not in store["profiles"]:
            raise HTTPException(status_code=404, detail="Profile not found")
        store["profiles"][pid]["name"] = _valid_profile_name(store, body.name, exclude=pid)
        _save_store(store)
        return _public_config(_load_config())

    @router.delete("/profiles/{pid}")
    async def delete_profile(pid: str):
        store = _load_store()
        if pid not in store["profiles"]:
            raise HTTPException(status_code=404, detail="Profile not found")
        if len(store["profiles"]) == 1:
            raise HTTPException(status_code=400, detail="Can't delete the last profile")
        del store["profiles"][pid]
        if store["active_profile"] == pid:
            store["active_profile"] = next(iter(store["profiles"]))
        _save_store(store)
        return _public_config(_load_config())

    @router.post("/keys/{provider}")
    async def submit_key(provider: str, body: KeySubmit):
        """Validate a key against its provider before storing it."""
        providers = {
            "novita": ("Novita", "api_key", _validate_novita_key),
            "civitai": ("Civitai", "civitai_api_key", _validate_civitai_key),
            "hf": ("Hugging Face", "hf_api_key", _validate_hf_key),
        }
        if provider not in providers:
            raise HTTPException(status_code=404, detail="Unknown provider")
        name, cfg_key, validate = providers[provider]
        key = body.api_key.strip()
        if not key or key.startswith(KEY_MASK_PREFIX):
            raise HTTPException(status_code=400, detail="Paste a key first")
        try:
            valid = await validate(key)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        if not valid:
            raise HTTPException(
                status_code=400,
                detail=f"{name} rejected this key — check for typos and make sure the whole key was copied")
        cfg = _load_config()
        cfg[cfg_key] = key
        _save_config(cfg)
        return _public_config(cfg)

    @router.get("/images")
    async def list_images(save_id: str | None = None, limit: int = 200):
        records = _read_index()
        if save_id:
            records = [r for r in records if r.get("save_id") == save_id]
        records = records[-max(1, min(500, limit)):]
        records.reverse()
        pending = sum(1 for r in records if r.get("status") in ("pending", "prompting", "generating"))
        # The chat footer conceals images per this mode; riding along on the
        # index it already polls saves it a second config request.
        conceal = _load_config().get("chat_image_conceal", "off")
        return {"records": records, "pending": pending, "chat_image_conceal": conceal}

    @router.get("/images/file/{filename}")
    async def get_image(filename: str):
        if not _FILENAME_RE.fullmatch(filename):
            raise HTTPException(status_code=404, detail="Not found")
        images_dir = (_data_dir() / "images").resolve()
        path = (images_dir / filename).resolve()
        if images_dir not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        # Filenames are immutable (uuid-suffixed), so long caching is safe.
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # ---- Character appearance tags (browsed/edited from character UIs) ----

    def _public_tag_entry(entry: dict, descriptor: str) -> dict:
        return {
            "tags": str(entry.get("tags") or ""),
            # Stale = keyed to an outdated descriptor: the prompt writer is
            # ignoring these tags and the backfill will regenerate them.
            "stale": entry.get("hash") != _appearance_hash(descriptor),
            "updated_at": entry.get("updated_at"),
            "source": str(entry.get("source") or "generated"),
        }

    @router.post("/character-tags/lookup")
    async def lookup_character_tags(req: TagLookup):
        """Cached appearance tags for the requested characters. A POST (not a
        GET) because staleness is computed against each character's current
        identity fields, which the caller supplies."""
        cfg = _load_config()
        entries = _read_tag_cache().get(str(req.save_id))
        if not isinstance(entries, dict):
            entries = {}
        tags = {}
        for ch in req.characters:
            entry = entries.get(ch.key)
            if isinstance(entry, dict) and str(entry.get("tags") or "").strip():
                descriptor = _character_descriptor(ch.race, ch.gender, ch.appearance)
                tags[ch.key] = _public_tag_entry(entry, descriptor)
        return {
            "tags_enabled": _prompt_style(cfg) == "tags"
                            and bool(cfg.get("character_reference_enabled", True)),
            "tags": tags,
        }

    @router.put("/character-tags/{save_id}/{key}")
    async def put_character_tags(save_id: str, key: str, update: TagUpdate):
        """Manually set (or, with empty tags, clear) a character's cached
        appearance tags. Saving stamps the hash of the character's current
        descriptor, so the edit counts as fresh and the backfill leaves it
        alone until the appearance changes again; clearing lets the next
        backfill regenerate from scratch. Synchronous read-modify-write (no
        awaits), so it cannot interleave with the backfill pipeline's."""
        raw = update.tags.strip()
        cache = _read_tag_cache()
        entries = cache.get(str(save_id))
        if not isinstance(entries, dict):
            entries = {}
        if not raw:
            if key in entries:
                del entries[key]
                cache[str(save_id)] = entries
                _write_tag_cache(cache)
            return {"deleted": True}
        # No usage filter (cfg=None): a deliberate manual tag is kept even if
        # it is rare on the booru sites.
        tags = _clean_character_tags(raw)
        if not tags:
            raise HTTPException(
                status_code=400,
                detail="No usable tags — scene/quality tags like 'solo' or "
                       "'masterpiece' are always stripped.")
        descriptor = _character_descriptor(update.race, update.gender, update.appearance)
        entries[key] = {
            "hash": _appearance_hash(descriptor),
            "tags": tags,
            "name": update.name,
            "updated_at": _now(),
            "source": "manual",
        }
        cache[str(save_id)] = entries
        _write_tag_cache(cache)
        return _public_tag_entry(entries[key], descriptor)

    @router.get("/models")
    async def search_models(query: str = "", cursor: str = "", limit: int = 48):
        cfg = _load_config()
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No API key configured")
        q, cur = query.strip(), cursor.strip()
        try:
            body = await _novita_list_models(cfg, q, cur, limit)
            models = _public_checkpoints(body)
            next_cursor = (body.get("pagination") or {}).get("next_cursor") or ""
            effective_query = q
            if not models and q and not cur:
                effective_query, models, next_cursor = (
                    await _novita_search_fallback(cfg, q, limit))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        # First-party FLUX.2 rides its own endpoint, so it is not in /v3/model;
        # pin it to the top of matching first pages.
        if not cur and (not q or "flux" in q.lower()):
            models.insert(0, {
                "sd_name": FLUX2_MODEL_NAME,
                "name": "FLUX.2 [dev] — Novita first-party (LoRAs via Civitai link)",
                "is_sdxl": False,
                "base_model": "Flux.2",
                "cover_url": None,
            })
        return {"models": models, "next_cursor": next_cursor,
                "effective_query": effective_query}

    @router.get("/civitai/loras")
    async def civitai_loras(query: str = "", base_model: str = "", lora_type: str = "LORA",
                            sort: str = "Most Downloaded", nsfw: str = "off",
                            category: str = "", cursor: str = "", limit: int = 24):
        cfg = _load_config()
        if nsfw not in CIVITAI_NSFW_MODES:
            nsfw = "off"
        if nsfw != "off" and not cfg.get("civitai_api_key"):
            raise HTTPException(status_code=400,
                                detail="NSFW browsing needs a Civitai API key")
        try:
            result = await _civitai_search_loras(
                cfg, query=query.strip(), base_model=base_model.strip(),
                lora_type=lora_type, sort=sort, nsfw_mode=nsfw,
                category=category.strip().lower(),
                cursor=cursor.strip(), limit=limit)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        _annotate_novita_availability(cfg, result["items"])
        return result

    @router.get("/hf/loras")
    async def hf_loras(query: str = "", base_model: str = "",
                       sort: str = "Most Downloaded", nsfw: str = "off",
                       cursor: str = "", limit: int = 24):
        cfg = _load_config()
        if nsfw not in CIVITAI_NSFW_MODES:
            nsfw = "off"
        try:
            result = await _hf_search_loras(
                cfg, query=query.strip(), base_model=base_model.strip(),
                sort=sort, nsfw_mode=nsfw, cursor=cursor.strip(),
                limit=max(1, min(100, limit)))
            await _hf_enrich_items(cfg, result["items"])
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        _annotate_novita_availability(cfg, result["items"])
        return result

    def _find_lora(cfg: dict, lora_id: str) -> dict:
        entry = next((e for e in cfg.get("lora_library") or []
                      if isinstance(e, dict) and e.get("id") == lora_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="LoRA not in library")
        return entry

    @router.post("/loras")
    async def save_lora(item: LoraSave):
        cfg = _load_config()
        library = cfg.get("lora_library") or []
        if any(isinstance(e, dict) and e.get("id") == item.id for e in library):
            raise HTTPException(status_code=409, detail="Already in library")
        if len(library) >= LORA_LIBRARY_MAX:
            raise HTTPException(status_code=400,
                                detail=f"Library is full ({LORA_LIBRARY_MAX} LoRAs)")
        if item.gated:
            raise HTTPException(
                status_code=400,
                detail="Gated Hugging Face repos can't be fetched by Novita")

        entry = _normalize_lora_entry(item.model_dump())
        if entry["source"] == "hf" and not _entry_hashes(entry):
            await _hf_refresh_entry_hashes(cfg, entry)
        # Flux LoRAs go to Novita as download links; only SD ones need to exist
        # in Novita's mirrored catalog.
        if _base_family(entry.get("base_model")) != "flux" and cfg.get("api_key"):
            try:
                entry["novita"] = await _novita_match_lora(cfg, entry)
            except RuntimeError as e:
                print(f"[Image Gen] Novita match failed for {entry['id']}: {e}")
            else:
                entry["novita_checked_at"] = _now()

        cfg = _load_config()  # re-load: the match awaited, config may have moved
        library = cfg.get("lora_library") or []
        if not any(isinstance(e, dict) and e.get("id") == entry["id"] for e in library):
            library.append(entry)
        cfg["lora_library"] = library
        _save_config(cfg)
        return {"entry": entry, "lora_library": library}

    @router.post("/loras/match_all")
    async def rematch_all_loras():
        cfg = _load_config()
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No Novita API key configured")
        pending = [
            dict(e) for e in cfg.get("lora_library") or []
            if isinstance(e, dict)
            and _base_family(e.get("base_model")) != "flux"
            and not e.get("novita")
            and not str(e.get("sd_name_override") or "").strip()
        ]
        results: dict = {}
        if pending:
            try:
                index = await _novita_lora_index(cfg, force=True)
            except RuntimeError as e:
                raise HTTPException(status_code=502, detail=str(e))
            for entry in pending:
                results[entry["id"]] = await _novita_match_lora(cfg, entry, index)

        cfg = _load_config()  # matches awaited; re-load before mutating
        now = _now()
        matched = 0
        for entry in cfg.get("lora_library") or []:
            if isinstance(entry, dict) and entry.get("id") in results:
                entry["novita"] = results[entry["id"]]
                entry["novita_checked_at"] = now
                if results[entry["id"]]:
                    matched += 1
        _save_config(cfg)
        return {"lora_library": cfg["lora_library"], "matched": matched,
                "checked": len(results)}

    @router.get("/novita/my-loras")
    async def my_novita_loras():
        """The account's own console-uploaded LoRAs, for linking to library
        entries that are not in Novita's public mirror."""
        cfg = _load_config()
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No API key configured")
        try:
            body = await _novita_list_models(cfg, "", "", 100,
                                             types="lora", visibility="private")
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        loras = [
            {
                "sd_name": m.get("sd_name_in_api") or m.get("sd_name"),
                "name": m.get("name") or m.get("sd_name") or "",
                "base_model": m.get("base_model") or "",
                "ready": m.get("status") == 1,
            }
            for m in (body.get("models") or [])
            if m.get("sd_name_in_api") or m.get("sd_name")
        ]
        return {"loras": loras, "max_slots": NOVITA_UPLOAD_SLOTS}

    @router.get("/loras/{lora_id}/download")
    async def download_lora(lora_id: str):
        """Redirect to the source's file download (with the user's token for
        Civitai), so the browser can grab the .safetensors for a manual Novita
        console upload without the key ever reaching the client."""
        cfg = _load_config()
        entry = _find_lora(cfg, lora_id)
        url = _lora_download_link(entry, cfg)
        if not url:
            raise HTTPException(status_code=404, detail="No download link for this LoRA")
        return RedirectResponse(url)

    @router.post("/loras/{lora_id}/match")
    async def rematch_lora(lora_id: str):
        cfg = _load_config()
        entry = _find_lora(cfg, lora_id)
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No Novita API key configured")
        try:
            index = await _novita_lora_index(cfg, force=True)
            match = await _novita_match_lora(cfg, entry, index)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        cfg = _load_config()
        entry = _find_lora(cfg, lora_id)
        entry["novita"] = match
        entry["novita_checked_at"] = _now()
        _save_config(cfg)
        return {"entry": entry, "lora_library": cfg["lora_library"]}

    @router.patch("/loras/{lora_id}")
    async def patch_lora(lora_id: str, patch: LoraPatch):
        cfg = _load_config()
        entry = _find_lora(cfg, lora_id)
        if patch.active is not None:
            entry["active"] = bool(patch.active)
        if patch.strength is not None:
            entry["strength"] = _clamp_lora_weight(patch.strength)
        if patch.sd_name_override is not None:
            entry["sd_name_override"] = patch.sd_name_override.strip()
        if patch.condition is not None:
            entry["condition"] = patch.condition.strip()
        if patch.llm_mode is not None:
            if patch.llm_mode not in LORA_LLM_MODES:
                raise HTTPException(
                    status_code=400,
                    detail=f"llm_mode must be one of: {', '.join(LORA_LLM_MODES)}")
            entry["llm_mode"] = patch.llm_mode
            entry.pop("llm_weight", None)  # superseded pre-mode flag
        if patch.trained_words is not None:
            # Trigger words are pulled from the model page but are often wrong
            # or missing; let the user correct them. Same shape as intake:
            # trimmed, non-empty, capped.
            entry["trained_words"] = [
                str(w).strip() for w in patch.trained_words if str(w).strip()][:20]
        _save_config(cfg)
        return {"entry": entry, "lora_library": cfg["lora_library"]}

    @router.delete("/loras/{lora_id}")
    async def delete_lora(lora_id: str):
        cfg = _load_config()
        _find_lora(cfg, lora_id)
        cfg["lora_library"] = [e for e in cfg["lora_library"]
                               if not (isinstance(e, dict) and e.get("id") == lora_id)]
        _save_config(cfg)
        return {"lora_library": cfg["lora_library"]}

    @router.post("/generate")
    async def generate(req: GenerateRequest):
        cfg = _load_config()
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No API key configured")
        if not cfg.get("model_name"):
            raise HTTPException(status_code=400, detail="No model selected — search and pick one first")

        save_id = req.save_id
        turn = 0
        narration = ""
        history_text = ""
        characters = None
        prompt_override = (req.prompt_override or "").strip() or None

        if req.retry_record_id:
            record = next((r for r in _read_index() if r.get("id") == req.retry_record_id), None)
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")
            save_id = record.get("save_id")
            turn = record.get("turn", 0)
            narration = record.get("narration_excerpt", "")
            prompt_override = prompt_override or record.get("image_prompt") or None
            if not prompt_override and not narration:
                raise HTTPException(status_code=400, detail="Record has nothing to retry from")
        elif prompt_override is None:
            session_manager = _services.get("session_manager")
            state = getattr(session_manager, "state", None) or {}
            history = state.get("history", [])
            if not history:
                raise HTTPException(status_code=400, detail="No story to illustrate — provide a prompt")
            narration = str(history[-1])
            history_text = "\n".join(str(h) for h in history[-6:-1])
            turn = state.get("turn", 0)
            save_id = save_id or getattr(session_manager, "active_save_id", None) or "unknown"
            characters = _character_snapshot(state)
            _spawn_tag_backfill(save_id, characters, None)
        else:
            save_id = save_id or "__studio__"
            if req.refine:
                # The typed text becomes the scene; the pipeline's prompt
                # writer refines it exactly like a story illustration (trigger
                # words, quality tags, style suffix, conditional LoRAs).
                narration = prompt_override
                prompt_override = None

        record_id = _spawn_generation(
            save_id=save_id, turn=turn, narration=narration, history=history_text,
            sdk=None, trigger="studio" if save_id == "__studio__" else "manual",
            prompt_override=prompt_override, characters=characters,
        )
        if record_id is None:
            raise HTTPException(status_code=409, detail="A generation is already running")
        if req.retry_record_id:
            # The replacement record carries the same excerpt/turn; leaving the
            # old one behind would render both under the message. Retrying an
            # error clears the error row; regenerating a finished image
            # replaces it, file included.
            async with _get_index_lock():
                records = _read_index()
                old = next((r for r in records if r.get("id") == req.retry_record_id), None)
                if old is not None and old.get("status") in ("error", "done"):
                    _write_index([r for r in records if r.get("id") != req.retry_record_id])
                else:
                    old = None
            if old is not None:
                _delete_record_files(old)
        return {"record_id": record_id}

    @router.delete("/images/{record_id}")
    async def delete_image(record_id: str):
        async with _get_index_lock():
            records = _read_index()
            record = next((r for r in records if r.get("id") == record_id), None)
            if record is None:
                raise HTTPException(status_code=404, detail="Record not found")
            records = [r for r in records if r.get("id") != record_id]
            _write_index(records)
        _delete_record_files(record)
        return {"ok": True}

    return router
