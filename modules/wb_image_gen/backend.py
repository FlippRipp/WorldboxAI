"""Image Generation -- illustrates the story with text-to-image.

Every N storyteller generations (or on demand via /image) the latest narration
is condensed into an image prompt by the smartest LLM slot, then rendered by
one of two providers behind the _generate_image dispatch (a small in-module
branch; two providers and one call site don't earn a registry):

- "novita": Novita AI's async cloud API -- submit, poll, download as one
  retryable unit (a failed task cannot be re-polled and its presigned result
  URL expires). Thousands of Civitai-mirrored checkpoints via the /models
  proxy; SD LoRAs resolve through Novita's mirrored catalog, FLUX.2 rides its
  own first-party endpoint with URL LoRAs. Prompts are capped at
  MAX_PROMPT_CHARS (a Novita API limit).
- "local": an A1111/Forge-compatible WebUI (--api) at a configured base URL.
  txt2img there is one synchronous call; /models lists the installed
  checkpoints and /civitai/checkpoints browses Civitai for new ones; LoRAs
  apply as <lora:name:weight> prompt tags added at payload time only, linked
  to library entries by hashing the configured LoRA folder (one-click
  installs download Civitai/HF checkpoints and LoRAs straight into the
  WebUI's folders). No prompt cap and no content filter, so the
  refusal-softening path never triggers.

The whole pipeline runs as a fire-and-forget background task so the player
keeps playing while the image renders; the chat-feed footer widget polls the
module's index and shows the image under the turn it illustrates.

Config is global (one provider choice and one set of keys for all stories),
owned by this module and edited in the Image Studio main-menu screen -- not in
per-save settings.
"""
import asyncio
import csv
import hashlib
import json
import os
import re
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

MODULE_ID = "wb_image_gen"

NOVITA_BASE = "https://api.novita.ai"
CIVITAI_BASE = "https://civitai.com/api/v1"

# Image providers: Novita's cloud API or a local A1111/Forge-compatible WebUI
# (AUTOMATIC1111, SD WebUI Forge, reForge, SD.Next) started with --api.
PROVIDERS = ("novita", "local")
LOCAL_DEFAULT_BASE = "http://127.0.0.1:7860"

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
    "SD 1.5", "SDXL 1.0", "Pony", "Illustrious", "NoobAI", "Anima",
    "Flux.1 D", "Flux.2 D",
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
    "NoobAI": "base_model:Laxhar/noobai-XL-1.1",
    "Anima": "base_model:circlestone-labs/Anima",
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

# Checkpoint -> Civitai page/preview matching for the model picker. Novita's
# truncated hash_sha256 and an A1111 title [shorthash] are both the first 10
# hex chars of the file SHA256 -- exactly Civitai's AutoV2 hash, which
# /model-versions/by-hash resolves to the model's page and preview images.
# Answers cache on disk: hits forever (model pages don't move), misses for a
# TTL (the file may get published on Civitai later). Transport failures cache
# nothing but pause further lookups briefly, so an unreachable Civitai cannot
# stall every /models search.
CIVITAI_CKPT_META_FILE = "civitai_ckpt_meta.json"
CIVITAI_HASH_MISS_TTL_S = 7 * 24 * 3600
CIVITAI_HASH_CONCURRENCY = 8
CIVITAI_HASH_BACKOFF_S = 120

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
# Static fallback for the scheduler dropdown (A1111 1.9+ label set). The
# local WebUI reports its own list via /local/schedulers; pre-1.9 WebUIs
# have no scheduler API at all and the field is never sent to them.
SCHEDULERS = [
    "Automatic",
    "Uniform",
    "Karras",
    "Exponential",
    "SGM Uniform",
    "Simple",
    "Normal",
    "DDIM",
    "Beta",
]
# Hires fix (two-pass upscale, the standard fine-detail pass for SDXL
# checkpoints): render at base size, upscale hires_scale x, re-diffuse at
# low denoise. Local provider only, like the scheduler. The fallback
# upscaler list mirrors what A1111/Forge ship built-in; the WebUI's real
# list comes from /local/upscalers. R-ESRGAN 4x+ Anime6B is bundled and
# the community default for anime checkpoints.
UPSCALERS = [
    "Latent",
    "Lanczos",
    "Nearest",
    "ESRGAN_4x",
    "R-ESRGAN 4x+",
    "R-ESRGAN 4x+ Anime6B",
    "SwinIR 4x",
]
DEFAULT_HIRES_UPSCALER = "R-ESRGAN 4x+ Anime6B"
HIRES_SCALE_MIN, HIRES_SCALE_MAX = 1.0, 4.0
HIRES_STEPS_MAX = 150
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
    "guidance_scale", "sampler_name", "scheduler",
    "hires_enabled", "hires_scale", "hires_upscaler", "hires_steps",
    "hires_denoise", "negative_prompt", "style_suffix",
    "quality_tags", "booru_subject_mode", "booru_break_separator",
    "tag_usage_filter", "tag_usage_min_count", "prompt_template",
    "prompt_template_tags", "prompt_style_mode",
)
GLOBAL_FIELDS = (
    "enabled", "api_key", "civitai_api_key", "hf_api_key", "interval",
    "step_retries", "prompt_model_preference", "beat_planner",
    "character_reference_enabled",
    "player_in_images", "chat_image_conceal", "civitai_nsfw",
    "provider", "local_base_url", "local_auth_user", "local_auth_pass",
    "local_checkpoint_dir", "local_lora_dir", "local_upscaler_dir",
    "local_text_encoder_dir", "local_vae_dir",
    "local_helper_url", "local_helper_token", "local_batch_size",
)
LORA_STATE_FIELDS = ("active", "strength", "llm_mode", "condition")
PROFILES_MAX = 20
PROFILE_NAME_MAX = 60

# Character appearances come from the optional wb_character_tracker /
# wb_npc_system modules. The roster is deliberately uncapped: every present
# character reaches the prompt writer and the LoRA gate in full (see
# CLAUDE.md -- no token caps on LLM input context).
PLAYER_IN_IMAGES_MODES = ("show", "pov")
# Multi-image batches: how the shared beat plan is written. "fast" uses the
# fastest LLM slot (default -- the plan is a simple chronology split and the
# extra call adds latency before the writers), "smart" the configured prompt
# writer slot, "off" skips planning (each writer splits the scene itself).
BEAT_PLANNER_MODES = ("off", "fast", "smart")
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
# The local WebUI's txt2img is synchronous and a render can take minutes on a
# weak GPU; batch slots also queue serially inside the WebUI, so a slot's wall
# time includes its siblings'.
LOCAL_TXT2IMG_TIMEOUT_S = 900.0
LOCAL_API_TIMEOUT_S = 15.0         # listing/options/refresh calls
# Pipeline-level retries: how many EXTRA times each step (prompt writing;
# submit+poll+download as one unit, since a failed task cannot be re-polled
# and result URLs expire) is re-run after a retryable failure.
STEP_RETRIES_DEFAULT = 1
STEP_RETRIES_MAX = 5
STEP_RETRY_BASE_DELAY_S = 2.0
# How many images one generation may render. Each image is its own Novita
# task, submitted and polled concurrently, so a batch takes roughly as long
# as a single image (but costs one generation per image; the local WebUI
# renders them one after another instead).
IMAGE_NUM_MAX = 8

# True GPU batching for the local provider. The plain txt2img API takes ONE
# prompt string, but with the bundled wb_prompt_batch.py script installed in
# the WebUI's scripts/ folder a whole multi-image generation renders as one
# request per LoRA-set group, each a single denoising batch of different
# prompts. local_batch_size caps images per batch (VRAM), and without the
# script everything falls back to the serial per-image requests.
LOCAL_BATCH_SCRIPT_TITLE = "WorldBox Prompt Batch"
LOCAL_BATCH_SCRIPT_FILE = "wb_prompt_batch.py"
LOCAL_BATCH_SIZE_DEFAULT = 4
LOCAL_SCRIPTS_PROBE_TTL_S = 300.0

# Each image in a batch gets its own prompt-writer call, so the images differ
# in content, not just seed. The hint steers WHICH moment of the scene each
# prompt depicts: a batch splits the latest scene into as many consecutive
# beats as there are images, so side by side the images read as a sequence
# of what happened; a single image is pointed at the scene's most striking
# beat instead -- left alone, the writer gravitates to the scene's final
# lines, which often land after the action has already resolved.
SINGLE_MOMENT_HINT = (
    "MOMENT CHOICE: depict the single most striking moment of the latest "
    "scene -- the peak of its action or emotion -- wherever it falls in the "
    "text. Scenes often keep going after the interesting part, so do not "
    "default to illustrating how the scene ends.")

# The shared beat plan: one LLM call splits the scene into N beats BEFORE the
# writers run, so all of them work from the same chronology. Without it each
# writer splits the scene on its own and the beat boundaries disagree, so a
# "later" image can depict an earlier moment than its neighbor.
BEAT_PLAN_PROMPT = """You are planning an illustrated sequence of {total} images for the scene below. Split the LATEST SCENE into exactly {total} consecutive beats in story order -- together they must cover the scene's arc, and its most striking action must land inside its own beat, never summarized away.

Rules:
- Each beat is ONE line: "N. " followed by 1-2 sentences of concrete visual description (who is visible, what they are doing, where, the mood). No inner thoughts, no story commentary.
- Strict story order: beat 1 is the earliest moment illustrated, beat {total} the latest.
- Output ONLY the {total} numbered lines, nothing else.

LATEST SCENE:
{narration}"""


def _parse_beat_plan(raw: str, total: int) -> list[str] | None:
    """The planner's numbered lines as a list, or None when the reply does
    not contain exactly beats 1..total (falls back to independent splits)."""
    beats: dict[int, str] = {}
    for line in (raw or "").splitlines():
        m = re.match(r"\s*(\d+)\s*[.):-]\s*(.*\S)", line)
        if m:
            n = int(m.group(1))
            if 1 <= n <= total and n not in beats:
                beats[n] = m.group(2).strip()
    if len(beats) != total:
        return None
    return [beats[i] for i in range(1, total + 1)]


async def _plan_beats(cfg: dict, narration: str, sdk, total: int) -> list[str] | None:
    """Split the latest scene into `total` consecutive beats with one LLM
    call, on the slot the beat_planner mode picks. "off", a single image, or
    any failure returns None, which falls back to each writer splitting the
    scene on its own."""
    mode = cfg.get("beat_planner", "fast")
    if mode == "off" or total <= 1 or sdk is None:
        return None
    preference = ("fastest" if mode == "fast"
                  else cfg.get("prompt_model_preference", "smartest"))
    prompt = (BEAT_PLAN_PROMPT
              .replace("{total}", str(total))
              .replace("{narration}", narration or ""))
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(prompt, model_preference=preference)
    except Exception as e:
        print(f"[Image Gen] Beat planning failed (independent splits instead): {e}")
        return None
    finally:
        sdk.llm._current_module = ""
    beats = _parse_beat_plan(raw, total)
    if beats is None:
        print("[Image Gen] Unparseable beat plan (independent splits instead): "
              f"{(raw or '')[:200]!r}")
    else:
        print(f"[Image Gen] Beat plan ({mode}, {total} beats): "
              + " | ".join(b[:60] for b in beats))
    return beats


def _moment_hint(slot: int, total: int, beats: list[str] | None = None) -> str:
    if total <= 1:
        return SINGLE_MOMENT_HINT
    preamble = (f"SEQUENCE: {total} prompts are being written independently "
                f"for this same scene, one image each, and side by side the "
                f"images must read as a chronological sequence of what "
                f"happened. ")
    if beats:
        plan = "\n".join(f"{i + 1}. {b}" for i, b in enumerate(beats))
        return (preamble +
                f"The scene is split into these consecutive beats:\n{plan}\n"
                f"Write this prompt for beat {slot + 1} ONLY -- depict that "
                f"beat's action, characters, and mood, and stay out of the "
                f"other beats.")
    position = (" This is the opening beat." if slot == 0 else
                " This is the final beat." if slot == total - 1 else "")
    return (preamble +
            f"Mentally split the latest scene into {total} consecutive "
            f"beats, in story order, and depict ONLY beat {slot + 1} of "
            f"{total}.{position} Stay inside that beat's action, characters, "
            f"and mood -- do not summarize the whole scene or drift into a "
            f"neighboring beat.")


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
- Output comma-separated booru tags, most important first: subject count (1girl, 1boy, 2girls, no humans...), then appearance (hair, eyes, clothing, species), action/pose, expression, setting, lighting, mood, composition.
- Include EXACTLY ONE framing tag matched to the moment: close-up, portrait, upper body, cowboy shot, full body, or wide shot. Default to the tightest framing that still shows the action (upper body or cowboy shot for dialogue and emotion); use full body or wide shot only when the whole figure or the scale of the scene is the point.
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
{narration}""",
                                # Pre-framing-rule default: without a required
                                # framing tag the writer rarely picks one and
                                # tag checkpoints drift to zoomed-out wides.
                                """You write prompts for an AI image generator that expects BOORU-STYLE TAGS (danbooru and e621 conventions). Turn the scene below into ONE comma-separated tag list depicting a single striking moment from the latest scene.

Rules:
- Output comma-separated booru tags, most important first: subject count (1girl, 1boy, 2girls, no humans...), then appearance (hair, eyes, clothing, species), action/pose, expression, setting, lighting, mood, composition (close-up, from above, wide shot...).
- Lowercase tag conventions: danbooru tags for human and anime-style subjects, e621 tags for anthro, feral, or creature subjects (anthro, feral, the species, fur and scale colors...). Mixing both vocabularies in one list is fine -- use whichever site's tag names the visual best.
- Concrete visual tags only -- no story summary, no proper-noun lore the image model cannot know; describe what things LOOK like instead.
- Output ONLY the tag list, no quotes, no preamble, 20-40 tags.

EARLIER CONTEXT (for continuity only):
{history}

LATEST SCENE (illustrate this):
{narration}""")

# Booru-tag checkpoint families each expect their own quality tags up front:
# score_* is Pony vocabulary; Illustrious/NoobAI/Animagine were trained on
# masterpiece/best-quality style tags (NoobAI also knows the "newest" recency
# tag); Anima (the CircleStone Labs 2B model, not SDXL) mixes both — its
# score scale runs 1..7, so score_7 is its top grade. Keyed by
# BOORU_TAG_MODEL_MARKERS entries. A stored quality_tags value
# still equal to ANY of these stock defaults was never customized, so it
# keeps tracking the active checkpoint's family (resolved in
# _effective_config); an edited value is used verbatim.
# Anima's card also recommends the "safe" rating tag in the positive prompt;
# it is deliberately excluded here because the module explicitly supports
# mature scenes — the rating belongs to the scene, not a stock prefix (the
# prompt writer can tag safe/sensitive/nsfw/explicit itself).
QUALITY_TAG_DEFAULTS = {
    "pony": "score_9, score_8_up, score_7_up",
    "illustrious": "masterpiece, best quality, very aesthetic, absurdres",
    "noob": "masterpiece, best quality, newest, absurdres, highres",
    "animagine": "masterpiece, best quality, very aesthetic, absurdres",
    "anima": "masterpiece, best quality, score_7",
}
DEFAULT_QUALITY_TAGS = QUALITY_TAG_DEFAULTS["pony"]
STOCK_QUALITY_TAGS = frozenset(QUALITY_TAG_DEFAULTS.values())

# Render settings per tag family, from each family's model card: the booru
# families were tuned around Euler a at CFG ~5-7 and publish their own
# negative-prompt vocabulary ("old"/"early" are NoobAI recency tags, the
# counterpart of "newest" above; score_6/5/4 is Pony's) -- the generic
# DPM++ 2M Karras at CFG 7 with a generic negative noticeably degrades
# NoobAI/Illustrious output. Same stock-value rule as QUALITY_TAG_DEFAULTS:
# a stored value still equal to ANY stock default was never customized and
# keeps tracking the active checkpoint's family; an edited value is used
# verbatim. NoobAI's card also lists anti-furry negatives (mammal, anthro,
# furry, feral); those are deliberately excluded because the module
# explicitly supports e621-style subjects.
DEFAULT_SAMPLER_NAME = "DPM++ 2M Karras"
DEFAULT_GUIDANCE_SCALE = 7.0
DEFAULT_NEGATIVE_PROMPT = "blurry, low quality, watermark, text, deformed"
DEFAULT_SCHEDULER = "Automatic"
RENDER_DEFAULTS = {
    "pony": {
        "sampler_name": "Euler a",
        "guidance_scale": 7.0,
        "scheduler": DEFAULT_SCHEDULER,
        "negative_prompt": ("score_6, score_5, score_4, worst quality, "
                            "low quality, jpeg artifacts, signature, "
                            "watermark, username"),
    },
    "illustrious": {
        "sampler_name": "Euler a",
        "guidance_scale": 6.0,
        "scheduler": DEFAULT_SCHEDULER,
        "negative_prompt": ("worst quality, low quality, lowres, bad anatomy, "
                            "bad hands, extra digits, jpeg artifacts, "
                            "signature, watermark, username"),
    },
    "noob": {
        "sampler_name": "Euler a",
        "guidance_scale": 5.0,
        "scheduler": DEFAULT_SCHEDULER,
        "negative_prompt": ("worst quality, old, early, low quality, lowres, "
                            "signature, username, logo, bad hands, "
                            "mutated hands, ambiguous form, watermark"),
    },
    "animagine": {
        "sampler_name": "Euler a",
        "guidance_scale": 6.0,
        "scheduler": DEFAULT_SCHEDULER,
        "negative_prompt": ("lowres, bad anatomy, bad hands, text, error, "
                            "missing fingers, extra digit, fewer digits, "
                            "cropped, worst quality, low quality, "
                            "jpeg artifacts, signature, watermark, username"),
    },
    # Anima's card: CFG 4-5 at 30-50 steps (the distilled Turbo variant wants
    # CFG 1 — that stays a per-profile tweak), samplers er_sde/euler_a/euler.
    # "Euler a" is the one of those every A1111-family WebUI ships; Forge
    # Neo's er_sde arrives through the dynamic /local/samplers list. The
    # negative is the card's verbatim, score_1..3 being the bottom of Anima's
    # 7-point scale and "artist name" its anti-signature tag. 4.0 rather than
    # 4.5 from the card's 4-5 range: it is already a stock guidance value
    # (VPRED_RENDER_OVERRIDES), so it widens STOCK_RENDER_SETTINGS by nothing
    # — a user's pinned 4.5 stays a customized value.
    "anima": {
        "sampler_name": "Euler a",
        "guidance_scale": 4.0,
        "scheduler": DEFAULT_SCHEDULER,
        "negative_prompt": ("worst quality, low quality, score_1, score_2, "
                            "score_3, artist name, blurry, jpeg artifacts, "
                            "chromatic aberration"),
    },
}
# v-pred finetunes (NoobAI-XL vPred and its merges, detected by _is_vpred)
# layer their own knobs on top of the family card: CFG must drop to 4.0
# (higher oversaturates without CFG-rescale, which the stock WebUI API cannot
# reach) and the noise schedule must be SGM Uniform (the Karras and Beta
# schedules break v-pred sampling). The override values are stock too, so
# eps<->vpred model switches keep tracking in both directions.
VPRED_RENDER_OVERRIDES = {"guidance_scale": 4.0, "scheduler": "SGM Uniform"}
STOCK_RENDER_SETTINGS = {
    field: frozenset({default,
                      *(d[field] for d in RENDER_DEFAULTS.values()),
                      *((VPRED_RENDER_OVERRIDES[field],)
                        if field in VPRED_RENDER_OVERRIDES else ())})
    for field, default in (("sampler_name", DEFAULT_SAMPLER_NAME),
                           ("guidance_scale", DEFAULT_GUIDANCE_SCALE),
                           ("negative_prompt", DEFAULT_NEGATIVE_PROMPT),
                           ("scheduler", DEFAULT_SCHEDULER))
}

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

# Multi-image batches from the studio Generate button carry no character
# roster, so each prompt writer would re-invent any unstated trait (hair
# color, outfit...) and the "same" character would drift between the batch's
# images. This one pre-pass fixes each described character's look for the
# whole batch. Unlike the cached canonical tags above, the outfit IS
# included: every image depicts this same scene, so the clothes must match
# too.
SCENE_CHARACTER_TAG_PROMPT = """Several images of the SAME scene are about to be generated, each from its own independently written prompt. Your job: fix each character's look ONCE, so every image depicts them identically.

From the scene below, list each distinct character it depicts (at most 3 -- the ones the scene centers on). For each, output ONE line:
<name or a short label like "the knight">: comma-separated lowercase booru-style appearance tags -- hair color, hair length and style, eye color, skin tone, species/race features (ears, horns, tail, fur, scales), build, age impression, notable marks, AND their outfit in this scene (clothing, armor, accessories, held items).
Where the scene leaves a trait unstated, INVENT a plausible one and state it -- an unstated hair color must not come out different in every image, so pick one now.
Output ONLY the character lines, nothing else. If the scene depicts no characters at all, output exactly: none

SCENE:
{narration}"""

_services: dict = {}
_tasks: set = set()
_hf_detail_cache: dict = {}   # repo_id -> (fetched_at, detail json)
_tag_dict_cache: dict[str, int] | None = None   # tag/alias -> booru post count
_gen_lock: asyncio.Lock | None = None
_index_lock: asyncio.Lock | None = None
_lora_index_lock: asyncio.Lock | None = None
_tag_lock: asyncio.Lock | None = None
_local_scan_lock: asyncio.Lock | None = None


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


def _shared_dir() -> Path:
    # API keys and service addresses are app-global, shared across
    # WB_DATA_DIR profile roots (demo mode included): credentials.json lives
    # under global_data_dir (the repo data dir, injected by the server)
    # rather than following the profile root the way config.json, images,
    # index, and caches (via _data_dir) do.
    base = _services.get("global_data_dir") or _services.get("data_dir")
    if base:
        root = Path(base) / MODULE_ID
    else:
        root = Path(__file__).resolve().parent.parent.parent / "data" / MODULE_ID
    root.mkdir(parents=True, exist_ok=True)
    return root


# The config fields shared across profile roots: the connection setup — API
# keys, the addresses of machine-local services along with the credentials
# to reach them, which provider is in use, and whether the module is on.
# Everything else in GLOBAL_FIELDS ("global" there means shared by the
# module's own generation profiles) follows the active data root.
SHARED_CONFIG_FIELDS = (
    "enabled", "provider",
    "api_key", "civitai_api_key", "hf_api_key",
    "local_base_url", "local_auth_user", "local_auth_pass",
    "local_helper_url", "local_helper_token",
)


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
        "guidance_scale": DEFAULT_GUIDANCE_SCALE,
        "sampler_name": DEFAULT_SAMPLER_NAME,
        "scheduler": DEFAULT_SCHEDULER,   # local only; Novita never sees it
        "hires_enabled": False,           # local only: two-pass hires-fix upscale
        "hires_scale": 1.5,
        "hires_upscaler": DEFAULT_HIRES_UPSCALER,
        "hires_steps": 14,                # second pass; 0 = reuse base steps
        "hires_denoise": 0.4,             # >0.5 risks anatomy drift
        "negative_prompt": DEFAULT_NEGATIVE_PROMPT,
        "interval": 3,
        "step_retries": STEP_RETRIES_DEFAULT,
        "prompt_model_preference": "smartest",
        "beat_planner": "fast",         # one of BEAT_PLANNER_MODES
        "prompt_template": DEFAULT_PROMPT_TEMPLATE,
        "prompt_template_tags": DEFAULT_PROMPT_TEMPLATE_TAGS,
        "quality_tags": DEFAULT_QUALITY_TAGS,
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
        "provider": "novita",           # one of PROVIDERS
        "local_base_url": LOCAL_DEFAULT_BASE,
        "local_auth_user": "",          # optional --api-auth credentials
        "local_auth_pass": "",          # masked like the API keys
        "local_checkpoint_dir": "",     # enables checkpoint installs from the browser
        "local_lora_dir": "",           # enables LoRA installs from the browser
        "local_upscaler_dir": "",       # empty derives models/ESRGAN from the checkpoint dir
        "local_text_encoder_dir": "",   # empty derives models/text_encoder (Anima's Qwen TE)
        "local_vae_dir": "",            # empty derives models/VAE (Anima's Qwen VAE)
        "local_helper_url": "",         # helper_server.py next to a remote WebUI
        "local_helper_token": "",       # its WB_HELPER_TOKEN; masked like keys
        "local_batch_size": LOCAL_BATCH_SIZE_DEFAULT,  # images per GPU batch (wb_prompt_batch.py)
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
    # quality_tags was pony_quality_tags before it went family-aware.
    if "quality_tags" not in stored \
            and isinstance(stored.get("pony_quality_tags"), str):
        profile["quality_tags"] = stored["pony_quality_tags"]
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


def _read_json_dict(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Image Gen] Failed to read {path.name}: {e}")
        return {}


def _load_store() -> dict:
    """The raw on-disk store: globals + shared lora_library + profiles, with
    SHARED_CONFIG_FIELDS resolved across profile roots. Per field: a
    non-empty credentials.json value wins; otherwise a value the profile's
    own config.json carries (pre-split installs); otherwise the global
    root's legacy config.json, so a fresh profile root (demo) sees the
    app's keys before the first post-split save. An empty credential never
    overrides a real value — a credentials.json written from a pristine
    profile root must not wipe keys that still live in a legacy config."""
    store = _load_profile_store()
    creds = _read_json_dict(_shared_dir() / "credentials.json")
    legacy = _read_json_dict(_shared_dir() / "config.json")
    defaults = _default_store()
    for k in SHARED_CONFIG_FIELDS:
        value = creds.get(k)
        if value not in (None, ""):
            store[k] = value
        elif store.get(k) in (None, "", defaults.get(k)) and legacy.get(k) not in (None, ""):
            store[k] = legacy[k]
    return store


def _load_profile_store() -> dict:
    """The per-data-root part of the store. Pre-profile flat files migrate
    in memory only (first save persists v2, same pattern as the legacy value
    migrations in _effective_config)."""
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
        # quality_tags was pony_quality_tags before it went family-aware.
        if "quality_tags" not in raw \
                and isinstance(raw.get("pony_quality_tags"), str):
            profile["quality_tags"] = raw["pony_quality_tags"]
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
    _atomic_write_json(_shared_dir() / "credentials.json",
                       {k: store[k] for k in SHARED_CONFIG_FIELDS if k in store})
    # Keys and addresses live only in the shared file, so profile roots
    # (e.g. the disposable demo dir) never hold secrets.
    _atomic_write_json(_data_dir() / "config.json",
                       {k: v for k, v in store.items() if k not in SHARED_CONFIG_FIELDS})


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
    # Configs from before the local provider existed have no provider field.
    if cfg.get("provider") not in PROVIDERS:
        cfg["provider"] = "novita"
    if cfg.get("player_in_images") not in PLAYER_IN_IMAGES_MODES:
        cfg["player_in_images"] = "show"
    if cfg.get("chat_image_conceal") not in CHAT_IMAGE_CONCEAL_MODES:
        cfg["chat_image_conceal"] = "off"
    if cfg.get("booru_subject_mode") not in BOORU_SUBJECT_MODES:
        cfg["booru_subject_mode"] = "single"
    if cfg.get("prompt_style_mode") not in PROMPT_STYLE_MODES:
        cfg["prompt_style_mode"] = "auto"
    if cfg.get("beat_planner") not in BEAT_PLANNER_MODES:
        cfg["beat_planner"] = "fast"
    # A stored tags template that still equals an old default was never
    # customized; keep it tracking the current default.
    if cfg.get("prompt_template_tags") in LEGACY_PROMPT_TEMPLATES_TAGS:
        cfg["prompt_template_tags"] = DEFAULT_PROMPT_TEMPLATE_TAGS
    # Same for quality tags: a stock value tracks the checkpoint family's own
    # default (score_* means nothing to NoobAI/Illustrious and vice versa).
    # Unrecognized families display the universal default; _quality_tags
    # withholds it at render time since their vocabulary is unknown.
    if cfg.get("quality_tags") in STOCK_QUALITY_TAGS:
        cfg["quality_tags"] = QUALITY_TAG_DEFAULTS.get(
            _tag_model_marker(cfg) or "", DEFAULT_QUALITY_TAGS)
    # Sampler, guidance scale, scheduler and negative prompt follow the
    # checkpoint family the same way (RENDER_DEFAULTS): a stored value still
    # equal to ANY stock default was never customized, so it keeps tracking
    # the active checkpoint's recommended settings. Unrecognized families
    # keep their values verbatim -- there is no render-time withholding like
    # _quality_tags, and snapping a hand-picked sampler back to the generic
    # default would discard user input on every Flux/SD1.5/unmarked
    # checkpoint.
    if not str(cfg.get("scheduler") or "").strip():
        cfg["scheduler"] = DEFAULT_SCHEDULER
    family_render = RENDER_DEFAULTS.get(_tag_model_marker(cfg) or "")
    if family_render:
        # v-pred finetunes layer their own guidance/scheduler on top of the
        # family card (see VPRED_RENDER_OVERRIDES).
        if _is_vpred(cfg):
            family_render = {**family_render, **VPRED_RENDER_OVERRIDES}
        for field, stock in STOCK_RENDER_SETTINGS.items():
            if cfg.get(field) in stock:
                cfg[field] = family_render[field]
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
    try:
        cfg["local_batch_size"] = max(1, min(IMAGE_NUM_MAX,
                                             int(cfg.get("local_batch_size"))))
    except (TypeError, ValueError):
        cfg["local_batch_size"] = LOCAL_BATCH_SIZE_DEFAULT
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


def _provider(cfg: dict) -> str:
    return "local" if cfg.get("provider") == "local" else "novita"


def _missing_setup(cfg: dict) -> str | None:
    """None when generation can run; otherwise the piece that's missing.
    Only Novita needs an API key — the local WebUI is keyless."""
    if _provider(cfg) == "novita" and not cfg.get("api_key"):
        return "api_key"
    if not cfg.get("model_name"):
        return "model_name"
    return None


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


def _get_local_scan_lock() -> asyncio.Lock:
    global _local_scan_lock
    if _local_scan_lock is None:
        _local_scan_lock = asyncio.Lock()
    return _local_scan_lock


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
# _base_family below. "anima" must stay AFTER "animagine" (its substring) and
# is matched on word boundaries only, so "Animagine", "animation..." and
# AnimaPencil-style SDXL names never read as CircleStone's Anima. Keep the
# mirror in ui/ImageStudio.jsx in sync.
BOORU_TAG_MODEL_MARKERS = ("pony", "illustrious", "noob", "animagine", "anima")

_ANIMA_WORD_RE = re.compile(r"\banima\b")


def _marker_matches(marker: str, ident: str) -> bool:
    if marker == "anima":
        return bool(_ANIMA_WORD_RE.search(ident))
    return marker in ident


def _tag_model_marker(cfg: dict) -> str | None:
    """The BOORU_TAG_MODEL_MARKERS entry the checkpoint matches, or None for
    natural-language models and unrecognized bases. Doubles as the key into
    QUALITY_TAG_DEFAULTS."""
    ident = _model_ident(cfg)
    return next((m for m in BOORU_TAG_MODEL_MARKERS
                 if _marker_matches(m, ident)), None)


# "vPred10", "v-pred", "v_pred", "v pred"... — the naming Civitai/HF releases
# actually use. Keep the mirror in ui/ImageStudio.jsx in sync.
_VPRED_RE = re.compile(r"v[\s_-]?pred", re.IGNORECASE)


def _is_vpred(cfg: dict) -> bool:
    """Whether the checkpoint self-identifies as v-prediction in its name or
    base metadata. Drives VPRED_RENDER_OVERRIDES and the /local/status
    diagnostics; rendering itself stays with the WebUI, which detects v-pred
    from the checkpoint file's own keys."""
    return bool(_VPRED_RE.search(_model_ident(cfg)))


def _prompt_style(cfg: dict) -> str:
    """Resolved prompt style, "tags" (danbooru lists) or "natural" (descriptive
    text). An explicit prompt_style_mode wins; "auto" picks "tags" for
    Pony/Illustrious/NoobAI/Animagine/Anima bases and "natural" for Flux and
    everything else. (Anima also understands natural language — forcing
    "natural" per profile is a supported choice there, its quality tags
    still apply.)"""
    mode = str(cfg.get("prompt_style_mode") or "auto")
    if mode in ("tags", "natural"):
        return mode
    return "tags" if _tag_model_marker(cfg) else "natural"


def _quality_tags(cfg: dict) -> str:
    """Quality-tag prefix for the prompt being written. A stock value tracks
    the checkpoint family's own default (normally pre-resolved by
    _effective_config; re-resolving here is idempotent and covers cfgs built
    from _default_config) and is withheld for unrecognized families, whose
    quality vocabulary is unknown. A customized value is the user's explicit
    choice: it applies verbatim to any recognized tag family (even under a
    forced natural prompt style -- Pony without score_* degrades whatever the
    prompt looks like) and to anything else running tag-style prompts."""
    value = str(cfg.get("quality_tags") or "").strip()
    marker = _tag_model_marker(cfg)
    if value in STOCK_QUALITY_TAGS:
        return QUALITY_TAG_DEFAULTS.get(marker or "", "")
    if not value:
        return ""
    if marker:
        return value
    return value if _prompt_style(cfg) == "tags" else ""


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
    covers everything trained on SDXL (Pony, Illustrious, NoobAI...); "anima"
    is CircleStone's 2B model — an architecture of its own, so its LoRAs and
    checkpoints only pair with each other. The xl/pony/... branch must stay
    ahead of the anima check: names like "Anima Pencil XL" are SDXL models,
    while Anima itself never carries an XL marker."""
    ident = str(base or "").lower()
    if "flux" in ident:
        return "flux"
    if ("xl" in ident or "pony" in ident or "illustrious" in ident
            or "noob" in ident or "animagine" in ident):
        return "sdxl"
    if _ANIMA_WORD_RE.search(ident):
        return "anima"
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


def _entry_local_name(entry: dict) -> str:
    """The name the local WebUI knows this LoRA file by (its stem), used in
    <lora:name:weight> prompt syntax. Set by an install, a hash scan of the
    LoRA folder, or a manual link in the Studio."""
    return str((entry.get("local") or {}).get("name") or "").strip()


def _entry_usable(entry: dict, family: str, provider: str = "novita") -> bool:
    """Active entry is usable when it matches the checkpoint family and has a
    way to reach the provider: locally an installed file (prompt syntax works
    for SD and Flux-under-Forge alike); on Novita a library name for SD, a
    download URL for Flux."""
    if _base_family(entry.get("base_model")) != family or not family:
        return False
    if provider == "local":
        return bool(_entry_local_name(entry))
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


def _local_prompt_lora_tags(cfg: dict) -> str:
    """<lora:name:weight> tags for the local WebUI, applied at payload time
    only — never fed to the prompt-writer LLM (which would mangle them), never
    run through tag-usage filtering, and never stored on records (so a retry's
    prompt_override cannot double-inject them). LLM gating and LLM-picked
    weights arrive here already applied to cfg's lora_library."""
    family = _checkpoint_family(cfg)
    tags = []
    for entry in _active_loras(cfg):
        if not _entry_usable(entry, family, "local"):
            continue
        weight = _clamp_lora_weight(entry.get("strength", LORA_DEFAULT_WEIGHT))
        tags.append(f"<lora:{_entry_local_name(entry)}:{weight}>")
    return " ".join(tags[:SD_LORAS_MAX])


def _applied_lora_names(cfg: dict) -> list[str]:
    family = _checkpoint_family(cfg)
    provider = _provider(cfg)
    return [
        str(entry.get("name") or entry.get("id") or "?")
        for entry in _active_loras(cfg)
        if _entry_usable(entry, family, provider)
    ][:FLUX_LORAS_MAX if family == "flux" and provider == "novita" else SD_LORAS_MAX]


def _union_lora_names(slot_cfgs: list[dict]) -> list[str]:
    """Every LoRA applied to at least one image of a batch, in first-
    appearance order -- the record stores one list for the whole batch even
    when per-beat gating gave each image its own set."""
    names: list[str] = []
    for c in slot_cfgs:
        for name in _applied_lora_names(c):
            if name not in names:
                names.append(name)
    return names


def _beat_scene(narration: str, beat: str) -> str:
    """The scene text a per-image LoRA gate judges: the full scene for
    context, plus the single beat this image actually depicts -- so a
    condition like 'a battle is happening' matches the duel image but not
    the farewell image of the same batch."""
    return (f"{narration}\n\nTHE IMAGE BEING ILLUSTRATED DEPICTS ONLY THIS "
            f"MOMENT OF THE SCENE: {beat}")


def _active_trigger_words(cfg: dict) -> tuple[list[str], list[str]]:
    """Trained trigger words of the LoRAs that will actually be applied, split
    by how the prompt writer should treat them: (mandatory, llm_picked).
    Mandatory words the writer must weave in verbatim; llm_picked words (from
    entries with triggers_llm on) are candidates the writer chooses from per
    image. Mandatory entries contribute their first few words as before;
    llm-picked entries contribute their full list -- the writer needs every
    candidate to choose well. Mandatory collects first so a word shared by
    both kinds of entry stays mandatory."""
    family = _checkpoint_family(cfg)
    provider = _provider(cfg)
    usable = [e for e in _active_loras(cfg)
              if _entry_usable(e, family, provider)]
    mandatory: list[str] = []
    llm_picked: list[str] = []
    seen: set[str] = set()

    def add(words, out):
        for word in words:
            word = str(word).strip().strip(",")
            if word and word.lower() not in seen:
                seen.add(word.lower())
                out.append(word)

    for entry in usable:
        if not entry.get("triggers_llm"):
            add((entry.get("trained_words") or [])[:4], mandatory)
    for entry in usable:
        if entry.get("triggers_llm"):
            add(entry.get("trained_words") or [], llm_picked)
    return mandatory[:12], llm_picked


def _render_template(template: str, narration: str, history: str) -> str:
    # Sequential replace instead of str.format: narration prose routinely
    # contains braces that would blow up format().
    out = template.replace("{narration}", narration)
    out = out.replace("{history}", history or "(story just began)")
    return out


def _prompt_cap(cfg: dict) -> int | None:
    """Novita rejects prompts over MAX_PROMPT_CHARS; a local WebUI chunks long
    prompts itself, so local mode must never truncate (see CLAUDE.md)."""
    return None if _provider(cfg) == "local" else MAX_PROMPT_CHARS


def _clean_image_prompt(raw: str, cap: int | None = None) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        first_line, _, rest = text.partition("\n")
        if first_line.strip().lower() in ("text", "prompt", "markdown"):
            text = rest
    text = text.strip().strip('"').strip()
    text = re.sub(r"\s+", " ", text)
    return text if cap is None else text[:cap]


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
              .replace("{narration}", narration or "")
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


async def _scene_character_notes(cfg: dict, narration: str, sdk) -> str:
    """Appearance notes for a multi-image batch that carries no character
    roster (the studio Generate button): one LLM pass reads the scene text
    and fixes each described character's look -- inventing any unstated
    trait so it cannot vary -- and every writer gets the SAME notes, keeping
    the character consistent across the sequence. Empty string when the
    scene has no characters or on any failure (writers then describe
    characters independently, as before)."""
    if sdk is None:
        return ""
    prompt = SCENE_CHARACTER_TAG_PROMPT.replace("{narration}", narration or "")
    try:
        sdk.llm._current_module = MODULE_ID
        raw = await sdk.llm.generate(
            prompt, model_preference=cfg.get("prompt_model_preference", "smartest"))
    except Exception as e:
        print(f"[Image Gen] Scene character notes failed (skipping): {e}")
        return ""
    finally:
        sdk.llm._current_module = ""
    raw = (raw or "").strip()
    if not raw or _looks_like_llm_refusal(raw):
        return ""
    # One "label: tags" line per character; anything else (a "none" reply,
    # stray commentary) simply drops out.
    lines = [ln.strip().lstrip("-* ").strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ":" in ln]
    if not lines:
        return ""
    notes = "\n".join(f"- {ln}" for ln in lines)
    print(f"[Image Gen] Scene character notes for this batch:\n{notes}")
    return ("CHARACTER CONSISTENCY (MANDATORY): several prompts are being "
            "written independently for images of this SAME scene, and the "
            "appearance notes below were fixed once for the whole batch. "
            "When you depict a character listed here, use their noted traits "
            "EXACTLY -- never re-invent or contradict hair, eyes, skin, "
            "species features, or outfit:\n" + notes)


async def _write_image_prompt(cfg: dict, narration: str, history: str, sdk,
                              characters: dict | None = None,
                              moment_hint: str = "",
                              character_notes: str = "",
                              insist: bool = False) -> str:
    style = _prompt_style(cfg)
    if style == "tags":
        template = cfg.get("prompt_template_tags") or DEFAULT_PROMPT_TEMPLATE_TAGS
    else:
        template = cfg.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE
    prompt = _render_template(template, narration, history)
    subject_mode = _subject_mode(cfg, characters)
    if subject_mode == "single":
        prompt += "\n\n" + BOORU_SINGLE_SUBJECT_RULE
    elif subject_mode == "multi":
        rule = BOORU_MULTI_SUBJECT_RULE
        if cfg.get("booru_break_separator"):
            rule += "\n" + BOORU_BREAK_RULE
        prompt += "\n\n" + rule
    triggers, llm_triggers = _active_trigger_words(cfg)
    if triggers:
        prompt += ("\n\nMANDATORY: weave these trigger words into the output verbatim "
                   "(they activate style adapters): " + ", ".join(triggers))
    if llm_triggers:
        prompt += ("\n\nTRIGGER CHOICE: each of these trigger words activates one "
                   "specific concept of a style adapter. Weave into the output, "
                   "verbatim, ONLY the ones that fit the scene being depicted, "
                   "and leave the rest out: " + ", ".join(llm_triggers))
    prompt += _character_block(cfg, characters, subject_mode)
    if character_notes:
        prompt += "\n\n" + character_notes
    if moment_hint:
        prompt += "\n\n" + moment_hint
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
        image_prompt = _filter_tags_by_usage(image_prompt, cfg,
                                             whitelist=triggers + llm_triggers)

    # Tag-trained checkpoints expect their family's quality tags up front.
    prefix = _quality_tags(cfg)
    suffix = str(cfg.get("style_suffix") or "").strip()

    cap = _prompt_cap(cfg)
    if cap is not None:
        # Trim the scene text, never the prefix/suffix, to fit Novita's cap.
        reserved = (len(prefix) + 2 if prefix else 0) + (len(suffix) + 2 if suffix else 0)
        image_prompt = image_prompt[:max(0, cap - reserved)].rstrip(", ")
    pieces = [p for p in (prefix, image_prompt, suffix) if p]
    joined = ", ".join(pieces)
    return joined if cap is None else joined[:cap]


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
    softened = _clean_image_prompt(raw, cap=_prompt_cap(cfg))
    if not softened or _looks_like_llm_refusal(softened):
        raise RuntimeError("prompt softener returned no usable prompt")
    return softened


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


class LocalNotFoundError(RuntimeError):
    """A local WebUI route answered 404. Capability probes catch this to tell
    "this WebUI genuinely lacks the endpoint" (a real, cacheable answer) apart
    from transport failures; plain-RuntimeError handlers keep working."""


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
    if _checkpoint_family(cfg) == "anima":
        # Novita's SD catalog cannot serve Anima's architecture; the request
        # would only fail with an opaque "model not found".
        raise NonRetryableError(
            "Anima checkpoints are not hosted on Novita — switch this "
            "profile's provider to Local and run a WebUI that supports "
            "Anima (Forge Neo; see image_server.sh/.bat)")
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


# --------------------------------------------------------------------------
# Local WebUI client (A1111 / Forge / reForge / SD.Next, /sdapi/v1/*)
# --------------------------------------------------------------------------

def _local_base(cfg: dict) -> str:
    return str(cfg.get("local_base_url") or LOCAL_DEFAULT_BASE).strip().rstrip("/")


def _local_auth(cfg: dict):
    """Basic-auth credentials when the WebUI runs with --api-auth."""
    user = str(cfg.get("local_auth_user") or "").strip()
    return (user, str(cfg.get("local_auth_pass") or "")) if user else None


def _local_error_detail(resp) -> str:
    """One legible line from a WebUI error: FastAPI's {"detail": ...},
    A1111's {"error", "errors"} shape, or a non-JSON body."""
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "").strip()[:300] or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body.get("errors")
        if detail:
            return str(detail)[:300]
        return str(body)[:300]
    return str(body)[:300]


def _local_unreachable(cfg: dict, error: Exception) -> str:
    return (f"Could not reach the local Stable Diffusion WebUI at "
            f"{_local_base(cfg)} — is it running with --api? ({error})")


async def _local_request(cfg: dict, method: str, path: str,
                         timeout: float = LOCAL_API_TIMEOUT_S):
    """Call a /sdapi route and return parsed JSON; every failure becomes one
    RuntimeError with the actionable connection hint."""
    import httpx
    url = f"{_local_base(cfg)}{path}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0),
                                     auth=_local_auth(cfg)) as client:
            resp = await client.request(method, url)
    except httpx.TransportError as e:
        raise RuntimeError(_local_unreachable(cfg, e))
    if resp.status_code in (401, 403):
        raise RuntimeError("The local WebUI rejected the credentials "
                           "(--api-auth username/password)")
    if resp.status_code == 404:
        raise LocalNotFoundError(f"{path} not found at {_local_base(cfg)} — "
                                 "launch the WebUI with --api")
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Local WebUI error {resp.status_code}: "
                           f"{_local_error_detail(resp)}")
    try:
        return resp.json()
    except Exception:
        return None


async def _local_get(cfg: dict, path: str, timeout: float = LOCAL_API_TIMEOUT_S):
    return await _local_request(cfg, "GET", path, timeout)


async def _local_post(cfg: dict, path: str, timeout: float = LOCAL_API_TIMEOUT_S):
    return await _local_request(cfg, "POST", path, timeout)


def _infer_local_base(ident: str) -> str:
    """Best-effort base-model metadata for a locally-installed checkpoint from
    its filename — the WebUI API exposes none. Values match Civitai baseModel
    strings so _base_family and BOORU_TAG_MODEL_MARKERS resolve; the Image
    Studio base-model select overrides this per profile when a name gives the
    heuristics nothing to work with."""
    low = str(ident or "").lower()
    if "pony" in low:
        return "Pony"
    if "illustrious" in low or "ilxl" in low or "illust" in low:
        return "Illustrious"
    if "noob" in low:
        return "NoobAI"
    if "animagine" in low:
        return "Animagine XL"
    if "flux" in low:
        return "Flux.1 D"
    if "xl" in low:
        return "SDXL 1.0"
    # After the xl check on purpose: "AnimaPencil XL"-style SDXL names must
    # resolve SDXL; the official Anima files ("anima-base-v1.0",
    # "anima-aesthetic-v1.1"...) carry no xl marker.
    if _ANIMA_WORD_RE.search(low):
        return "Anima"
    if re.search(r"sd ?_?1[-.]?5|v1-5|\b1\.5\b", low):
        return "SD 1.5"
    return ""


def _webui_model_hash(m: dict) -> str:
    """AutoV2 prefix for one /sdapi/v1/sd-models entry: its sha256/hash field
    or the title's [shorthash] — all SHA256 prefixes in A1111 forks, computed
    lazily by the WebUI so any of the three may be missing."""
    bracket = _CKPT_TITLE_HASH_RE.search(str(m.get("title") or ""))
    for h in (m.get("sha256"), m.get("hash"),
              bracket.group(0).strip(" []") if bracket else ""):
        prefix = _hash_prefix10(h)
        if prefix:
            return prefix
    return ""


async def _local_list_checkpoints(cfg: dict) -> list[dict]:
    """Installed checkpoints in the model-picker entry shape /models returns.
    `title` (filename plus the WebUI's short hash once computed) is what
    override_settings.sd_model_checkpoint accepts. The WebUI has no covers or
    pages; /models fills cover_url/civitai_url from Civitai by file hash."""
    body = await _local_get(cfg, "/sdapi/v1/sd-models")
    models = []
    for m in body if isinstance(body, list) else []:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or m.get("model_name") or "").strip()
        if not title:
            continue
        base = _infer_local_base(f"{title} {m.get('model_name') or ''}")
        models.append({
            "sd_name": title,
            "name": str(m.get("model_name") or title),
            "is_sdxl": _base_family(base) == "sdxl",
            "base_model": base,
            "cover_url": None,
            "hash": _webui_model_hash(m),
            "civitai_url": "",
        })
    return models


# Batch-script probe results by base URL ({"ok": bool, "at": monotonic s}),
# so a generation doesn't re-ask /sdapi/v1/scripts for every batch.
_local_scripts_probe: dict = {}


async def _local_batch_script_available(cfg: dict, force: bool = False) -> bool:
    """Whether the WebUI lists wb_prompt_batch.py as a txt2img script. Probe
    failures return False WITHOUT caching -- batching is an optimization and
    one hiccup must not disable it for a whole TTL window -- while real
    answers (installed or not) cache briefly."""
    base = _local_base(cfg)
    cached = _local_scripts_probe.get(base)
    if not force and cached \
            and time.monotonic() - cached["at"] < LOCAL_SCRIPTS_PROBE_TTL_S:
        return cached["ok"]
    try:
        body = await _local_get(cfg, "/sdapi/v1/scripts")
    except Exception:
        return False
    names = body.get("txt2img") if isinstance(body, dict) else None
    # Compare lowercased on both sides: the endpoint reports lowercased
    # titles on some forks and verbatim titles on others.
    ok = LOCAL_BATCH_SCRIPT_TITLE.lower() in {
        str(n).strip().lower() for n in (names or [])}
    _local_scripts_probe[base] = {"ok": ok, "at": time.monotonic()}
    return ok


# Scheduler-support probe results by base URL ({"labels": list|None, "at":
# monotonic s}). Pre-1.9 A1111 has no /sdapi/v1/schedulers and 422s on a
# payload "scheduler" field, so the field is only sent after a probe.
_local_schedulers_probe: dict = {}


async def _local_list_schedulers(cfg: dict, force: bool = False) -> list[str] | None:
    """Scheduler labels from /sdapi/v1/schedulers, or None when the WebUI has
    no scheduler API (sending the payload field there would 422). A 404 is a
    real answer and caches for the TTL; transport failures return None
    WITHOUT caching, mirroring _local_batch_script_available -- the scheduler
    is an enhancement and one hiccup must not disable it for a TTL window."""
    base = _local_base(cfg)
    cached = _local_schedulers_probe.get(base)
    if not force and cached \
            and time.monotonic() - cached["at"] < LOCAL_SCRIPTS_PROBE_TTL_S:
        return cached["labels"]
    try:
        body = await _local_get(cfg, "/sdapi/v1/schedulers")
    except LocalNotFoundError:
        _local_schedulers_probe[base] = {"labels": None, "at": time.monotonic()}
        return None
    except Exception:
        return None
    labels = [str(s.get("label") or s.get("name")).strip()
              for s in (body if isinstance(body, list) else [])
              if isinstance(s, dict) and (s.get("label") or s.get("name"))]
    _local_schedulers_probe[base] = {"labels": labels, "at": time.monotonic()}
    return labels


async def _local_scheduler_ok(cfg: dict) -> bool:
    """Whether the payload may carry cfg's scheduler: skipped without any
    HTTP for empty/Automatic (nothing to send -- the WebUI's automatic choice
    IS the default), otherwise gated on the schedulers API existing."""
    scheduler = str(cfg.get("scheduler") or "").strip()
    if not scheduler or scheduler.lower() == "automatic":
        return False
    return await _local_list_schedulers(cfg) is not None


# Anima checkpoints bake in no text encoder or VAE: Forge-family WebUIs load
# both as "additional modules" (the UI's VAE / Text Encoder multiselect),
# which the API selects per request via full file paths in
# override_settings.forge_additional_modules. /sdapi/v1/sd-modules lists the
# installed candidates with those paths; classic A1111 has no such endpoint
# (and cannot run Anima at all).
ANIMA_TEXT_ENCODER_FILE = "qwen_3_06b_base.safetensors"
ANIMA_VAE_FILE = "qwen_image_vae.safetensors"
_local_modules_probe: dict = {}


async def _local_list_modules(cfg: dict, force: bool = False) -> list[dict] | None:
    """VAE / text-encoder modules from /sdapi/v1/sd-modules as {"name",
    "filename"} dicts, or None when the WebUI has no modules API. Caching
    mirrors _local_list_schedulers: a 404 is a real answer and caches for the
    TTL, transport failures return None WITHOUT caching."""
    base = _local_base(cfg)
    cached = _local_modules_probe.get(base)
    if not force and cached \
            and time.monotonic() - cached["at"] < LOCAL_SCRIPTS_PROBE_TTL_S:
        return cached["modules"]
    try:
        body = await _local_get(cfg, "/sdapi/v1/sd-modules")
    except LocalNotFoundError:
        _local_modules_probe[base] = {"modules": None, "at": time.monotonic()}
        return None
    except Exception:
        return None
    modules = []
    for item in body if isinstance(body, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("model_name") or item.get("name") or "").strip()
        filename = str(item.get("filename") or item.get("path") or "").strip()
        if name or filename:
            modules.append({"name": name, "filename": filename})
    _local_modules_probe[base] = {"modules": modules, "at": time.monotonic()}
    return modules


def _match_anima_modules(modules: list[dict]) -> list[str]:
    """Full paths of the Qwen text encoder and VAE among the WebUI's modules,
    text encoder first; missing pieces are simply absent from the result.
    Matched by "qwen" in the stem rather than the exact official filenames so
    the community 4B encoder builds work too — sd-modules only ever lists
    VAE/text-encoder files, so the net is safe to cast this wide."""
    te_path = vae_path = ""
    for module in modules:
        stem = Path(str(module.get("filename") or module.get("name") or "")
                    .replace("\\", "/")).stem.lower()
        path = str(module.get("filename") or "")
        if "qwen" not in stem or not path:
            continue
        if "vae" in stem:
            vae_path = vae_path or path
        else:
            te_path = te_path or path
    return [p for p in (te_path, vae_path) if p]


async def _local_anima_modules(cfg: dict) -> list[str] | None:
    """The forge_additional_modules value for an Anima render: both Qwen
    module paths when the WebUI has them, None to leave the payload alone
    when the user has manually selected modules in the WebUI (they may use
    files this matcher doesn't know). Raises NonRetryableError with the
    actionable diagnosis when Anima cannot render at all."""
    modules = await _local_list_modules(cfg)
    if modules is None:
        raise NonRetryableError(
            "This WebUI has no VAE/text-encoder module API, so it cannot "
            "run Anima checkpoints — use SD WebUI Forge Neo "
            "(image_server.sh/.bat installs it)")
    paths = _match_anima_modules(modules)
    if len(paths) == 2:
        return paths
    # The Qwen files aren't installed (or carry unrecognizable names). If the
    # user picked modules in the WebUI's own UI, honor that selection instead
    # of failing; with nothing selected the render would only die inside the
    # WebUI with a cryptic missing-state-dict error.
    try:
        options = await _local_get(cfg, "/sdapi/v1/options")
    except Exception:
        options = None
    selected = (options or {}).get("forge_additional_modules") \
        if isinstance(options, dict) else None
    if isinstance(selected, list) and selected:
        return None
    raise NonRetryableError(
        f"Anima needs its text encoder ({ANIMA_TEXT_ENCODER_FILE}) and VAE "
        f"({ANIMA_VAE_FILE}) installed next to the WebUI — install both "
        "from the Image Studio's Setup tab (or drop them into the WebUI's "
        "models/text_encoder and models/VAE folders)")


# The WebUI's APIs expose no usable file hashes (/sdapi/v1/loras only has
# kohya's sshs_model_hash, a weights hash — never the file SHA256 that
# Civitai/HF publish) — so matching browse results and library entries to
# installed files means hashing the model folders ourselves. Results are
# cached by (size, mtime) so rescans only hash new or changed files. LoRAs
# and checkpoints index into separate cache files, one per folder.
LOCAL_HASH_CACHE_FILE = "local_hash_cache.json"
LOCAL_CKPT_HASH_CACHE_FILE = "local_ckpt_hash_cache.json"
LOCAL_SCAN_EXTS = (".safetensors", ".ckpt", ".pt")
# Browse annotation re-scans a folder in the background when its cache is
# older than this, so files the user dropped in by hand get badged too.
LOCAL_HASH_RESCAN_S = 900

# The one-click install targets: which config key holds the folder, which
# cache file its hashes live in, which WebUI endpoint rescans it ("" = the
# WebUI has no rescan route for this kind and needs a restart), and which
# file extensions an install may write.
LOCAL_UPSCALER_HASH_CACHE_FILE = "local_upscaler_hash_cache.json"
LOCAL_INSTALL_KINDS = {
    "lora": {"dir_key": "local_lora_dir",
             "cache_file": LOCAL_HASH_CACHE_FILE,
             "refresh_path": "/sdapi/v1/refresh-loras",
             "label": "LoRA",
             "exts": (".safetensors", ".ckpt"),
             "default_ext": ".safetensors"},
    "checkpoint": {"dir_key": "local_checkpoint_dir",
                   "cache_file": LOCAL_CKPT_HASH_CACHE_FILE,
                   "refresh_path": "/sdapi/v1/refresh-checkpoints",
                   "label": "checkpoint",
                   "exts": (".safetensors", ".ckpt"),
                   "default_ext": ".safetensors"},
    # ESRGAN-family hires-fix upscalers (models/ESRGAN). No refresh route
    # exists for them — A1111/Forge scan the folder at startup only, so a
    # fresh install needs a WebUI restart before it can render.
    "upscaler": {"dir_key": "local_upscaler_dir",
                 "cache_file": LOCAL_UPSCALER_HASH_CACHE_FILE,
                 "refresh_path": "",
                 "label": "upscaler",
                 "exts": (".pth", ".pt", ".safetensors"),
                 "default_ext": ".pth"},
    # Standalone text encoders and VAEs (models/text_encoder, models/VAE) —
    # what Anima's Qwen files install as. Forge-family WebUIs list both
    # through the same modules dropdown and rescan them via refresh-vae.
    "text_encoder": {"dir_key": "local_text_encoder_dir",
                     "cache_file": "local_te_hash_cache.json",
                     "refresh_path": "/sdapi/v1/refresh-vae",
                     "label": "text encoder",
                     "exts": (".safetensors", ".ckpt"),
                     "default_ext": ".safetensors"},
    "vae": {"dir_key": "local_vae_dir",
            "cache_file": "local_vae_hash_cache.json",
            "refresh_path": "/sdapi/v1/refresh-vae",
            "label": "VAE",
            "exts": (".safetensors", ".ckpt", ".pt"),
            "default_ext": ".safetensors"},
}

# One-click install catalog for Anima's support files, mirroring
# UPSCALER_CATALOG: the exact files the official repo ships, URLs and SHA256s
# verified against the Hugging Face API. Forge Neo lists both in its
# VAE / Text Encoder dropdown once installed.
ANIMA_MODULE_CATALOG = [
    {"name": "Qwen3 0.6B text encoder",
     "kind": "text_encoder",
     "filename": ANIMA_TEXT_ENCODER_FILE,
     "url": "https://huggingface.co/circlestone-labs/Anima/resolve/main/"
            "split_files/text_encoders/qwen_3_06b_base.safetensors",
     "sha256": "cd2a512003e2f9f3cd3c32a9c3573f820bb28c940f73c57b1ddaa983d9223eba",
     "size": 1192135096,
     "description": "The text encoder every Anima checkpoint prompts "
                    "through (~1.1 GB)"},
    {"name": "Qwen-Image VAE",
     "kind": "vae",
     "filename": ANIMA_VAE_FILE,
     "url": "https://huggingface.co/circlestone-labs/Anima/resolve/main/"
            "split_files/vae/qwen_image_vae.safetensors",
     "sha256": "a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f",
     "size": 253806246,
     "description": "The image decoder Anima renders through (~250 MB)"},
]

# Curated hires-fix upscalers for one-click install, so nobody has to hunt
# these down by hand. URLs and SHA256s verified against the Hugging Face
# API; all are 4x ESRGAN-architecture models (~64 MB) that A1111/Forge load
# from models/ESRGAN. The WebUI shows each by its filename stem.
UPSCALER_CATALOG = [
    {"name": "4x-AnimeSharp",
     "filename": "4x-AnimeSharp.pth",
     "url": "https://huggingface.co/Kim2091/AnimeSharp/resolve/main/4x-AnimeSharp.pth",
     "sha256": "e7a7de2dafd7331c1992862bbbcd9e9712a9f9f8e6303f0aaa59b4341d359bab",
     "size": 67010245,
     "description": "Anime line art and cel shading — the go-to for "
                    "Illustrious/NoobAI hires fix"},
    {"name": "4x-UltraSharp",
     "filename": "4x-UltraSharp.pth",
     "url": "https://huggingface.co/Kim2091/UltraSharp/resolve/main/4x-UltraSharp.pth",
     "sha256": "a5812231fc936b42af08a5edba784195495d303d5b3248c24489ef0c4021fe01",
     "size": 66961958,
     "description": "Crisp general-purpose detail — the de-facto community "
                    "default hires upscaler"},
    {"name": "4x_foolhardy_Remacri",
     "filename": "4x_foolhardy_Remacri.pth",
     "url": "https://huggingface.co/FacehugmanIII/4x_foolhardy_Remacri/resolve/main/4x_foolhardy_Remacri.pth",
     "sha256": "e1a73bd89c2da1ae494774746398689048b5a892bd9653e146713f9df8bca86a",
     "size": 67025055,
     "description": "Sharp, less smoothed detail on photo and anime alike"},
    {"name": "4x_NMKD-Siax_200k",
     "filename": "4x_NMKD-Siax_200k.pth",
     "url": "https://huggingface.co/gemasai/4x_NMKD-Siax_200k/resolve/main/4x_NMKD-Siax_200k.pth",
     "sha256": "560424d9f68625713fc47e9e7289a98aabe1d744e1cd6a9ae5a35e9957fd127e",
     "size": 66957746,
     "description": "Clean general detail reconstruction, a good all-rounder"},
    {"name": "4x_NMKD-Superscale-SP_178000_G",
     "filename": "4x_NMKD-Superscale-SP_178000_G.pth",
     "url": "https://huggingface.co/uwg/upscaler/resolve/main/ESRGAN/4x_NMKD-Superscale-SP_178000_G.pth",
     "sha256": "1d1b0078fe71446e0469d8d4df59e96baa80d83cda600d68237d655830821bcc",
     "size": 66958607,
     "description": "Neutral 'super-scale' detail, a popular alternative to "
                    "UltraSharp"},
]


def _read_local_hash_cache(cache_file: str = LOCAL_HASH_CACHE_FILE) -> dict | None:
    path = _data_dir() / cache_file
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Image Gen] Failed to read {cache_file}: {e}")
        return None
    return data if isinstance(data, dict) and isinstance(data.get("files"), dict) \
        else None


def _hash_cache_stale(cache: dict | None) -> bool:
    if cache is None:
        return True
    try:
        scanned = datetime.fromisoformat(str(cache.get("scanned_at") or ""))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - scanned).total_seconds() > LOCAL_HASH_RESCAN_S


def _local_hash_index(cache: dict | None) -> dict[str, str] | None:
    """sha256 -> file stem (the WebUI's <lora:...> name) from a scan cache;
    None when no scan has ever run (unknown, not 'nothing installed')."""
    if cache is None:
        return None
    index: dict[str, str] = {}
    for file_path, meta in cache.get("files", {}).items():
        sha = str((meta or {}).get("sha256") or "").lower()
        if sha:
            index[sha] = Path(file_path).stem
    return index


def _stem_hash_index(cache: dict | None) -> dict[str, str]:
    """The reverse of _local_hash_index — file stem (casefolded) -> AutoV2
    prefix — for model-picker entries whose WebUI title carries no hash yet:
    the scan cache knows the file's SHA256 by name."""
    index: dict[str, str] = {}
    for file_path, meta in (cache or {}).get("files", {}).items():
        prefix = _hash_prefix10((meta or {}).get("sha256"))
        if prefix:
            index.setdefault(Path(file_path).stem.casefold(), prefix)
    return index


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Sanity cap for a safetensors header; real headers are a few MB at most,
# so anything bigger means a corrupt or non-safetensors file.
_SAFETENSORS_HEADER_MAX = 100 * (1 << 20)


def _safetensors_header_keys(path: Path) -> frozenset[str] | None:
    """Key names from a safetensors header (tensor names plus __metadata__
    keys — tools stash flags like v_pred in either place), reading only the
    8-byte length prefix and the JSON header, never the tensors. None for
    non-.safetensors files, unreadable files, or implausible headers."""
    if path.suffix.lower() != ".safetensors":
        return None
    try:
        with open(path, "rb") as f:
            (length,) = struct.unpack("<Q", f.read(8))
            if not 0 < length <= _SAFETENSORS_HEADER_MAX:
                return None
            header = json.loads(f.read(length).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, struct.error):
        return None
    if not isinstance(header, dict):
        return None
    meta = header.get("__metadata__")
    return frozenset(header) | frozenset(meta if isinstance(meta, dict) else ())


_CKPT_TITLE_HASH_RE = re.compile(r"\s*\[[0-9a-f]{6,12}\]\s*$", re.I)


def _local_checkpoint_file(cfg: dict) -> Path | None:
    """The selected checkpoint's file under local_checkpoint_dir, or None
    (dir unset/unreadable — e.g. a WebUI on another machine — or file not
    found). model_name for the local provider is the WebUI title: a path
    relative to the model folder plus an optional " [shorthash]" suffix."""
    root = _local_install_dir(cfg, "checkpoint")
    if root is None:
        return None
    name = _CKPT_TITLE_HASH_RE.sub("", str(cfg.get("model_name") or "")).strip()
    if not name:
        return None
    direct = root / Path(name.replace("\\", "/"))
    if direct.is_file():
        return direct
    # Titles are sometimes bare filenames while the file sits in a subfolder.
    try:
        return next(root.rglob(Path(name.replace("\\", "/")).name), None)
    except OSError:
        return None


def _vpred_checkpoint_diagnosis(cfg: dict) -> dict | None:
    """For a v-pred-named checkpoint whose file this machine can read:
    whether the file carries the "v_pred" key WebUIs auto-detect v-prediction
    from. None when there is nothing to say (not v-pred-named, remote WebUI,
    .ckpt file, unreadable header) — absence of evidence is not a warning."""
    if not _is_vpred(cfg):
        return None
    path = _local_checkpoint_file(cfg)
    if path is None:
        return None
    keys = _safetensors_header_keys(path)
    if keys is None:
        return None
    return {"file": path.name, "has_vpred_key": "v_pred" in keys}


async def _scan_local_hashes(folder: str,
                             cache_file: str = LOCAL_HASH_CACHE_FILE) -> dict:
    """Hash every model file under the folder (recursively — the WebUI
    supports subfolders), reusing cached digests for files whose size+mtime
    are unchanged. Persists and returns the refreshed cache."""
    root = Path(folder)
    old_files = (_read_local_hash_cache(cache_file) or {}).get("files", {})
    files: dict[str, dict] = {}
    for path in sorted(p for p in root.rglob("*")
                       if p.is_file() and p.suffix.lower() in LOCAL_SCAN_EXTS):
        try:
            stat = path.stat()
            prev = old_files.get(str(path))
            if (isinstance(prev, dict) and prev.get("size") == stat.st_size
                    and prev.get("mtime") == stat.st_mtime):
                files[str(path)] = prev
                continue
            digest = await asyncio.to_thread(_sha256_file, path)
        except OSError as e:
            print(f"[Image Gen] Could not hash {path}: {e}")
            continue
        files[str(path)] = {"size": stat.st_size, "mtime": stat.st_mtime,
                            "sha256": digest}
    cache = {"files": files, "scanned_at": _now()}
    _atomic_write_json(_data_dir() / cache_file, cache)
    return cache


def _register_local_file(path: Path,
                         cache_file: str = LOCAL_HASH_CACHE_FILE) -> None:
    """Add one just-installed file to the hash cache without a full rescan."""
    try:
        stat = path.stat()
        digest = _sha256_file(path)
    except OSError as e:
        print(f"[Image Gen] Could not hash {path}: {e}")
        return
    cache = _read_local_hash_cache(cache_file) or {"files": {}}
    cache["files"][str(path)] = {"size": stat.st_size, "mtime": stat.st_mtime,
                                 "sha256": digest}
    cache["scanned_at"] = _now()
    _atomic_write_json(_data_dir() / cache_file, cache)


def _match_local_hashes(index: dict[str, str], entry: dict) -> str | None:
    """Newest-version-first file-stem hit, mirroring _match_hashes."""
    for h in _entry_hashes(entry):
        stem = index.get(h)
        if stem:
            return stem
    return None


def _spawn_local_hash_scan(cfg: dict, kind: str = "lora") -> None:
    """Fire-and-forget model-folder scan so browse annotation never blocks on
    hashing; the scan lock dedupes concurrent runs."""
    spec = LOCAL_INSTALL_KINDS[kind]
    folder = str(cfg.get(spec["dir_key"]) or "").strip()
    if not folder or not Path(folder).is_dir():
        return
    if _get_local_scan_lock().locked():
        return

    async def _run():
        async with _get_local_scan_lock():
            try:
                await _scan_local_hashes(folder, spec["cache_file"])
            except OSError as e:
                print(f"[Image Gen] {spec['label']} folder scan failed: {e}")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return   # sync context (tests) — annotation degrades to "unknown"
    task = loop.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _annotate_local_availability(cfg: dict, items: list[dict],
                                 kind: str = "lora") -> None:
    """Badge browse results with whether the file is already installed in the
    local LoRA/checkpoint folder (matched by SHA256 against that folder's scan
    cache). No scan yet degrades to None ("unknown") while one runs in the
    background; a stale cache still annotates but refreshes behind the
    request so hand-copied files eventually get badged."""
    cache = _read_local_hash_cache(LOCAL_INSTALL_KINDS[kind]["cache_file"])
    index = _local_hash_index(cache)
    if index is None or _hash_cache_stale(cache):
        _spawn_local_hash_scan(cfg, kind)
    for item in items:
        if index is None or not _entry_hashes(item):
            item["local_available"] = None
            continue
        stem = _match_local_hashes(index, item)
        item["local_available"] = bool(stem)
        if stem:
            item["local_name"] = stem


# A1111's checkpoint shorthash and Civitai's AutoV2 hash are both the first
# 10 hex chars of the file SHA256, so they join directly (the same trick as
# NOVITA_HASH_PREFIX_LEN).
CKPT_HASH_PREFIX_LEN = 10
_CKPT_TITLE_HASH_RE = re.compile(r"\s*\[[0-9a-fA-F]{8,12}\]$")


def _ckpt_title_stem(title: str) -> str:
    """The bare file stem of a WebUI checkpoint title or path — subfolders,
    the extension, and a trailing " [shorthash]" stripped."""
    base = _CKPT_TITLE_HASH_RE.sub("", str(title or "").strip())
    base = base.replace("\\", "/").rsplit("/", 1)[-1]
    for ext in LOCAL_SCAN_EXTS:
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base


async def _local_api_checkpoint_index(cfg: dict) -> dict | None:
    """Hash-prefix and filename-stem indexes over the WebUI's installed
    checkpoints, built from /sdapi/v1/sd-models — how browse badges match
    when the checkpoint folder is not a path this machine can scan (the
    WebUI runs on another machine). The WebUI computes SHA256s lazily, so
    hash coverage grows as checkpoints get loaded; filename stems fill the
    gap for files that kept their Civitai names. None when unreachable."""
    try:
        body = await _local_get(cfg, "/sdapi/v1/sd-models")
    except RuntimeError:
        return None
    prefixes: dict[str, str] = {}
    stems: dict[str, str] = {}
    for m in body if isinstance(body, list) else []:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or m.get("model_name") or "").strip()
        stem = _ckpt_title_stem(str(m.get("filename") or "") or title)
        if not stem:
            continue
        bracket = _CKPT_TITLE_HASH_RE.search(str(m.get("title") or ""))
        for h in (m.get("sha256"), m.get("hash"),
                  bracket.group(0).strip(" []") if bracket else ""):
            h = str(h or "").strip().lower()
            if len(h) >= CKPT_HASH_PREFIX_LEN:
                prefixes.setdefault(h[:CKPT_HASH_PREFIX_LEN], stem)
        stems.setdefault(stem.casefold(), stem)
        model_name_stem = _ckpt_title_stem(str(m.get("model_name") or ""))
        if model_name_stem:
            stems.setdefault(model_name_stem.casefold(), stem)
    return {"prefixes": prefixes, "stems": stems}


def _match_api_checkpoint(index: dict, entry: dict) -> str | None:
    """First hash-prefix hit in the WebUI's model list, falling back to the
    Civitai file name (renamed files only match once their hash is known)."""
    for h in _entry_hashes(entry):
        stem = index["prefixes"].get(h[:CKPT_HASH_PREFIX_LEN])
        if stem:
            return stem
    file_stem = _ckpt_title_stem(str(entry.get("file_name") or ""))
    if file_stem:
        return index["stems"].get(file_stem.casefold())
    return None


# --------------------------------------------------------------------------
# Install helper client — helper_server.py running next to a remote WebUI
# gives one-click installs and exact hash badges across machines.
# --------------------------------------------------------------------------

HELPER_DEFAULT_PORT = 7861   # where image_server.sh/.bat start helper_server.py


def _helper_url(cfg: dict) -> str:
    return str(cfg.get("local_helper_url") or "").strip().rstrip("/")


def _helper_headers(cfg: dict) -> dict:
    token = str(cfg.get("local_helper_token") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _helper_request(cfg: dict, method: str, path: str,
                          json_body: dict | None = None,
                          timeout: float = 30.0) -> dict:
    """One call against the install helper; RuntimeError with an actionable
    message on any failure."""
    import httpx
    base = _helper_url(cfg)
    if not base:
        raise RuntimeError("No install helper configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            resp = await client.request(method, f"{base}{path}",
                                        json=json_body, headers=_helper_headers(cfg))
    except httpx.TransportError as e:
        raise RuntimeError(f"Could not reach the install helper at {base}: {e}")
    if resp.status_code == 401:
        raise RuntimeError("The install helper rejected the token — paste its "
                           "WB_HELPER_TOKEN into Setup")
    if resp.status_code >= 400:
        try:
            detail = str((resp.json() or {}).get("detail") or "")
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"Install helper error {resp.status_code}: {detail}")
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(f"The install helper at {base} answered with "
                           "invalid JSON — is something else on that port?")
    return body if isinstance(body, dict) else {}


async def _helper_hash_indexes(cfg: dict) -> dict:
    """The helper's SHA256 indexes of its folders: {"checkpoint": {sha ->
    file stem}, "lora": {...}, "scanning": bool}. While the helper's first
    scan is still hashing, absence from the index means "unknown", not "not
    installed". Raises RuntimeError when the helper is unreachable."""
    body = await _helper_request(cfg, "GET", "/wb-helper/hashes")
    kinds = body.get("kinds") if isinstance(body.get("kinds"), dict) else {}
    out: dict = {"scanning": bool(body.get("scanning"))}
    for kind in LOCAL_INSTALL_KINDS:
        index = kinds.get(kind)
        out[kind] = ({str(k).lower(): str(v) for k, v in index.items()}
                     if isinstance(index, dict) else {})
    return out


async def _detect_helper(cfg: dict) -> dict | None:
    """Probe the WebUI host's default helper port when no helper is
    configured — the launcher script starts one on HELPER_DEFAULT_PORT, so
    the connection test can offer it with one click instead of making the
    user find the URL. Returns {"url", "auth_required"} or None."""
    from urllib.parse import urlsplit
    base = urlsplit(_local_base(cfg))
    host = base.hostname or "127.0.0.1"
    candidate = f"{base.scheme or 'http'}://{host}:{HELPER_DEFAULT_PORT}"
    probe_cfg = {"local_helper_url": candidate,
                 "local_helper_token": cfg.get("local_helper_token", "")}
    try:
        health = await _helper_request(probe_cfg, "GET", "/wb-helper/health",
                                       timeout=5.0)
    except RuntimeError as e:
        # A live helper whose token we don't have is still worth reporting.
        if "rejected the token" in str(e):
            return {"url": candidate, "auth_required": True}
        return None
    if health.get("service") == "wb_image_gen_helper":
        return {"url": candidate, "auth_required": bool(health.get("auth"))}
    return None


# Folder kinds whose unset config field derives the WebUI-standard sibling
# of the checkpoint folder, so the bundled-launcher layout works with zero
# extra setup. The names match A1111/Forge/Forge Neo's models/ layout.
LOCAL_DERIVED_SIBLINGS = {"upscaler": "ESRGAN",
                          "text_encoder": "text_encoder",
                          "vae": "VAE"}


def _local_install_dir(cfg: dict, kind: str) -> Path | None:
    """The kind's install folder when this machine can write to it directly,
    else None (unset, or a path that only exists on the WebUI's machine).
    Unset upscaler/text-encoder/VAE folders derive the WebUI-standard
    sibling of the checkpoint folder (models/Stable-diffusion ->
    models/ESRGAN, models/text_encoder, models/VAE); the folder may not
    exist yet on a fresh WebUI — the install creates it."""
    dest = str(cfg.get(LOCAL_INSTALL_KINDS[kind]["dir_key"]) or "").strip()
    if dest and Path(dest).is_dir():
        return Path(dest)
    if kind in LOCAL_DERIVED_SIBLINGS and not dest:
        ckpt = Path(str(cfg.get("local_checkpoint_dir") or "").strip() or ".")
        if ckpt.name.lower() == "stable-diffusion" and ckpt.is_dir():
            return ckpt.parent / LOCAL_DERIVED_SIBLINGS[kind]
    return None


async def _annotate_browse_availability(cfg: dict, items: list[dict],
                                        kind: str = "lora") -> None:
    """Browse badges for the local provider, best source first: a hash scan
    of the folder when this machine can read it, the install helper's hash
    index when one is configured (WebUI on another machine), and — for
    checkpoints only — the WebUI's own model list as a fuzzy last resort."""
    _annotate_local_availability(cfg, items, kind=kind)
    if _helper_url(cfg) and any(i.get("local_available") is None for i in items):
        try:
            indexes = await _helper_hash_indexes(cfg)
        except RuntimeError as e:
            print(f"[Image Gen] Helper hash index failed: {e}")
            indexes = None
        if indexes is not None:
            for item in items:
                if item.get("local_available") is not None or not _entry_hashes(item):
                    continue
                stem = _match_local_hashes(indexes[kind], item)
                if stem:
                    item["local_available"] = True
                    item["local_name"] = stem
                elif not indexes["scanning"]:
                    # A finished helper scan is authoritative: absent = absent.
                    item["local_available"] = False
    if kind == "checkpoint":
        pending = [i for i in items if i.get("local_available") is None]
        if not pending:
            return
        index = await _local_api_checkpoint_index(cfg)
        if index is None:
            return   # WebUI unreachable: leave "unknown"
        for item in pending:
            stem = _match_api_checkpoint(index, item)
            item["local_available"] = bool(stem)
            if stem:
                item["local_name"] = stem


# --------------------------------------------------------------------------
# Local installs: download Civitai/HF files into the WebUI's model folders
# --------------------------------------------------------------------------

_downloads: dict[str, dict] = {}                 # id -> pollable status dict
_download_tasks: dict[str, "asyncio.Task"] = {}
# Helper downloads whose completion follow-ups (WebUI refresh, LoRA library
# link) already ran, so each poll of the merged list fires them only once.
_remote_done_seen: set = set()
DOWNLOAD_CHUNK = 1 << 20
DOWNLOADS_KEEP_FINISHED = 20
LOCAL_INSTALL_EXTS = (".safetensors", ".ckpt")
_CONTENT_DISPOSITION_RE = re.compile(r'filename\*?="?([^";]+)"?')


def _safe_install_filename(raw: str, fallback: str, kind: str = "lora") -> str:
    """A bare, whitelisted-extension filename that cannot escape the install
    folder. `raw` usually comes from Content-Disposition; the fallback is the
    slugged entry name. The extension whitelist is per kind — upscalers are
    .pth/.pt files, everything else safetensors/ckpt."""
    spec = LOCAL_INSTALL_KINDS.get(kind) or {}
    exts = tuple(spec.get("exts") or LOCAL_INSTALL_EXTS)
    name = Path(str(raw or "").strip().replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if not name or not name.lower().endswith(exts):
        base = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(fallback or "model")).strip(" .")
        # A fallback that already carries a whitelisted extension keeps it
        # (catalog entries pass exact filenames); otherwise the kind's
        # default extension is appended.
        if base.lower().endswith(exts):
            name = base
        else:
            name = (base or "model") + str(spec.get("default_ext") or ".safetensors")
    return name


def _prune_downloads() -> None:
    finished = sorted(
        (d for d in _downloads.values() if d["status"] != "downloading"),
        key=lambda d: str(d.get("completed_at") or ""))
    for stale in finished[:-DOWNLOADS_KEEP_FINISHED] if len(finished) > DOWNLOADS_KEEP_FINISHED else []:
        _downloads.pop(stale["id"], None)
        _download_tasks.pop(stale["id"], None)


def _spawn_remote_install_followup(cfg: dict, download: dict) -> None:
    """After the helper finishes an install on the WebUI's machine: make the
    WebUI rescan its folders and, for LoRAs, link the library entry to the
    new file — the same follow-ups the local pipeline runs, driven from the
    polling side since the bytes never touched this machine."""
    kind = download.get("kind") if download.get("kind") in LOCAL_INSTALL_KINDS else "lora"
    lora_id = str(download.get("lora_id") or "").strip()
    stem = Path(str(download.get("filename") or "").replace("\\", "/")).stem

    async def _run():
        refresh_path = LOCAL_INSTALL_KINDS[kind]["refresh_path"]
        if refresh_path:
            try:
                await _local_post(cfg, refresh_path, timeout=60.0)
            except RuntimeError as e:
                print(f"[Image Gen] Post-install refresh failed: {e}")
        if kind in ("text_encoder", "vae"):
            # Same as the local pipeline: force the next Anima render to
            # re-list sd-modules instead of a stale probe.
            _local_modules_probe.pop(_local_base(cfg), None)
        if kind == "lora" and lora_id and stem:
            await _link_installed_lora(lora_id, stem)

    task = asyncio.get_running_loop().create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _link_installed_lora(lora_id: str, stem: str) -> None:
    """Stamp the library entry with its just-installed local name (save_lora's
    reload-then-save pattern, since the download awaited across config
    writes)."""
    cfg = _load_config()
    for entry in cfg.get("lora_library") or []:
        if isinstance(entry, dict) and entry.get("id") == lora_id:
            entry["local"] = {"name": stem, "source": "install"}
            entry["local_checked_at"] = _now()
            _save_config(cfg)
            return


async def _download_file_pipeline(dl_id: str, url: str, dest_dir: Path,
                                  fallback_name: str, expected_hashes: list[str],
                                  refresh_path: str, lora_id: str | None,
                                  cfg: dict, kind: str = "lora") -> None:
    """Stream one file into the WebUI's model folder with byte progress and a
    running SHA256, then refresh the WebUI and link the library entry. Only
    ever marks its own status dict — never raises to the caller."""
    import httpx
    status = _downloads[dl_id]
    part_path: Path | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0),
                                     follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code == 401 and "civitai.com" in url:
                    raise RuntimeError("Civitai requires an API key for this "
                                       "download — add one in Image Studio")
                if resp.status_code != 200:
                    raise RuntimeError(f"Download failed (HTTP {resp.status_code})")
                disposition = resp.headers.get("content-disposition", "")
                match = _CONTENT_DISPOSITION_RE.search(disposition)
                filename = _safe_install_filename(
                    match.group(1) if match else "", fallback_name, kind=kind)
                final_path = (dest_dir / filename).resolve()
                if dest_dir.resolve() not in final_path.parents:
                    raise RuntimeError("Refusing a filename outside the install folder")
                status["filename"] = filename
                status["total_bytes"] = int(resp.headers.get("content-length") or 0)

                part_path = final_path.with_suffix(final_path.suffix + ".part")
                digest = hashlib.sha256()
                received = 0
                with open(part_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK):
                        f.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        status["received_bytes"] = received

        if expected_hashes and digest.hexdigest().lower() not in expected_hashes:
            raise RuntimeError("Downloaded file failed its SHA256 check — "
                               "the source may have served the wrong file")
        os.replace(part_path, final_path)
        part_path = None
        if lora_id:
            _register_local_file(final_path)
            await _link_installed_lora(lora_id, final_path.stem)
        elif kind in LOCAL_INSTALL_KINDS:
            # Learn the file so browse/catalog badges flip to "installed"
            # without waiting for the next folder rescan.
            _register_local_file(final_path, LOCAL_INSTALL_KINDS[kind]["cache_file"])
        if refresh_path:   # "" = no rescan route for this kind (upscalers)
            try:
                await _local_post(cfg, refresh_path, timeout=60.0)
            except RuntimeError as e:
                # The file is in place; the WebUI just hasn't rescanned yet.
                print(f"[Image Gen] Post-install refresh failed: {e}")
        if kind in ("text_encoder", "vae"):
            # The next Anima render must re-list sd-modules, not trust a
            # probe cached from before this file existed.
            _local_modules_probe.pop(_local_base(cfg), None)
        status["status"] = "done"
    except asyncio.CancelledError:
        status["status"] = "error"
        status["error"] = "cancelled"
        raise
    except Exception as e:
        status["status"] = "error"
        status["error"] = str(e)[:300]
        print(f"[Image Gen] Install {dl_id} failed: {e}")
    finally:
        status["completed_at"] = _now()
        if part_path is not None:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
        _prune_downloads()


def _local_prompt_with_tags(cfg: dict, image_prompt: str) -> str:
    """The final prompt string a local render sees: the written prompt plus
    cfg's <lora:...> tags. Both the single and the batched path go through
    here, so a batched image gets exactly the prompt its solo render would."""
    lora_tags = _local_prompt_lora_tags(cfg)
    if lora_tags:
        return f"{image_prompt} {lora_tags}" if image_prompt else lora_tags
    return image_prompt


def _local_payload(cfg: dict, image_prompt: str,
                   scheduler_ok: bool = False,
                   anima_modules: list[str] | None = None) -> dict:
    # No prompt cap here: local WebUIs chunk long prompts themselves.
    payload = {
        "prompt": _local_prompt_with_tags(cfg, image_prompt),
        "width": int(cfg.get("width", 1024)),
        "height": int(cfg.get("height", 1024)),
        "steps": int(cfg.get("steps", 28)),
        "cfg_scale": float(cfg.get("guidance_scale", 7.0)),
        "sampler_name": str(cfg.get("sampler_name", "DPM++ 2M Karras")),
        "seed": -1,
        "batch_size": 1,
        "n_iter": 1,
        "send_images": True,
        "save_images": False,
        # Selecting the checkpoint per request keeps profiles self-contained;
        # not restoring afterwards keeps it loaded across a batch. The two
        # extra pins neutralize stale WebUI globals that silently corrupt
        # renders: clip-skip back to 1 (the SDXL-correct value; a leftover 2
        # from SD1.5 use degrades output on WebUIs that apply it) and the VAE
        # to Automatic (prefers the checkpoint's baked/matching VAE; a stale
        # global VAE washes colors out). Deliberately not config-exposed
        # until someone actually needs to pin a custom VAE per profile.
        "override_settings": {
            "sd_model_checkpoint": str(cfg.get("model_name", "")),
            "CLIP_stop_at_last_layers": 1,
            "sd_vae": "Automatic",
        },
        "override_settings_restore_afterwards": False,
    }
    # Anima renders carry their Qwen text encoder + VAE as Forge additional
    # modules (resolved by _local_anima_modules); the sd_vae pin would fight
    # that selection, so it rides only for the SD families.
    if anima_modules:
        payload["override_settings"].pop("sd_vae", None)
        payload["override_settings"]["forge_additional_modules"] = list(anima_modules)
    negative = str(cfg.get("negative_prompt") or "").strip()
    if negative:
        payload["negative_prompt"] = negative
    # The scheduler field 422s on pre-1.9 A1111, so it rides only when the
    # caller probed support (_local_scheduler_ok). "Automatic" is what the
    # WebUI does without the field, so it is never sent.
    scheduler = str(cfg.get("scheduler") or "").strip()
    if scheduler_ok and scheduler and scheduler.lower() != "automatic":
        payload["scheduler"] = scheduler
    # Hires fix: render at base size, then upscale hires_scale x and
    # re-diffuse at low denoise — the standard fine-detail pass for SDXL
    # checkpoints. The enable_hr family of fields has existed since early
    # A1111, so no probe is needed; hr_additional_modules is Forge-specific
    # (some builds fail without the key) and unknown keys are ignored by
    # A1111's API models.
    if cfg.get("hires_enabled"):
        payload["enable_hr"] = True
        payload["hr_scale"] = float(cfg.get("hires_scale", 1.5))
        payload["hr_upscaler"] = str(cfg.get("hires_upscaler")
                                     or DEFAULT_HIRES_UPSCALER)
        payload["hr_second_pass_steps"] = int(cfg.get("hires_steps", 14))
        payload["denoising_strength"] = float(cfg.get("hires_denoise", 0.4))
        payload["hr_additional_modules"] = []
    return payload


async def _local_txt2img(cfg: dict, payload: dict) -> list[str]:
    """POST one txt2img request and return the raw base64 images list.
    Failures split into NonRetryableError vs RuntimeError by whether a
    resubmission of the same request could succeed."""
    import httpx
    url = f"{_local_base(cfg)}/sdapi/v1/txt2img"
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(LOCAL_TXT2IMG_TIMEOUT_S, connect=10.0),
                auth=_local_auth(cfg)) as client:
            resp = await client.post(url, json=payload)
    except httpx.ConnectError as e:
        # Retrying in seconds won't start the user's WebUI; fail the record
        # fast with the actionable message instead.
        raise NonRetryableError(_local_unreachable(cfg, e))
    except httpx.TransportError as e:
        # Timeout or reset mid-render: the next attempt may finish.
        raise RuntimeError(f"Local WebUI request failed: {e}")
    if resp.status_code in (401, 403):
        raise NonRetryableError("The local WebUI rejected the credentials "
                                "(--api-auth username/password)")
    if resp.status_code == 404:
        raise NonRetryableError(f"/sdapi/v1/txt2img not found at "
                                f"{_local_base(cfg)} — launch the WebUI with --api")
    if resp.status_code in (400, 422):
        raise NonRetryableError(f"The local WebUI rejected the request "
                                f"(HTTP {resp.status_code}): {_local_error_detail(resp)}")
    if resp.status_code >= 500:
        # Often a CUDA OOM or a mid-load hiccup; worth a resubmission.
        raise RuntimeError(f"Local WebUI server error {resp.status_code}: "
                           f"{_local_error_detail(resp)}")
    images = (resp.json() or {}).get("images") or []
    if not images:
        raise RuntimeError("The local WebUI returned no images")
    return [str(img) for img in images]


def _local_b64_decode(image: str) -> bytes:
    import base64
    # Some WebUIs return a data URI, others bare base64.
    return base64.b64decode(image.split(",", 1)[-1])


async def _local_render_modules(cfg: dict) -> list[str] | None:
    """The anima_modules payload argument for this render: resolved for Anima
    profiles, None (field absent) for every other family."""
    if _checkpoint_family(cfg) != "anima":
        return None
    return await _local_anima_modules(cfg)


async def _local_generate(cfg: dict, image_prompt: str) -> tuple[bytes, str]:
    """One synchronous txt2img render against the local WebUI."""
    payload = _local_payload(cfg, image_prompt,
                             scheduler_ok=await _local_scheduler_ok(cfg),
                             anima_modules=await _local_render_modules(cfg))
    images = await _local_txt2img(cfg, payload)
    return _local_b64_decode(images[0]), "png"


def _local_batch_payload(cfg: dict, image_prompts: list[str],
                         scheduler_ok: bool = False,
                         anima_modules: list[str] | None = None) -> dict:
    """A txt2img payload that renders every prompt in one GPU batch via the
    bundled wb_prompt_batch.py script. The script reads the JSON prompt list
    from script_args and sets batch_size itself; the top-level prompt and
    batch_size stay in their single-image shape so the request remains a
    valid (if single-image) txt2img body."""
    payload = _local_payload(cfg, image_prompts[0], scheduler_ok=scheduler_ok,
                             anima_modules=anima_modules)
    payload["script_name"] = LOCAL_BATCH_SCRIPT_TITLE
    payload["script_args"] = [json.dumps(
        [_local_prompt_with_tags(cfg, p) for p in image_prompts])]
    return payload


async def _local_generate_batch(cfg: dict,
                                image_prompts: list[str]) -> list[tuple[bytes, str]]:
    """One batched txt2img render: all prompts share a single GPU batch and
    therefore a single LoRA set -- the caller groups prompts so only cells
    with identical tag strings arrive here together."""
    payload = _local_batch_payload(cfg, image_prompts,
                                   scheduler_ok=await _local_scheduler_ok(cfg),
                                   anima_modules=await _local_render_modules(cfg))
    images = await _local_txt2img(cfg, payload)
    # The script suppresses the grid, but a fork that ignores
    # do_not_save_grid prepends one; drop it by count.
    if len(images) == len(image_prompts) + 1:
        images = images[1:]
    if len(images) != len(image_prompts):
        raise RuntimeError(
            f"the batch script returned {len(images)} images for "
            f"{len(image_prompts)} prompts — is {LOCAL_BATCH_SCRIPT_FILE} in "
            f"the WebUI's scripts folder up to date?")
    return [(_local_b64_decode(img), "png") for img in images]


def _local_error_looks_oom(error: BaseException) -> bool:
    """Whether a local render failure reads like the GPU ran out of memory --
    the one failure a smaller batch would fix and a same-size retry won't."""
    return bool(re.search(r"out of memory|outofmemory|allocat", str(error), re.I))


async def _generate_image(cfg: dict, image_prompt: str) -> tuple[bytes, str]:
    """One provider-agnostic image render — the pipeline's retryable unit.
    Novita is submit/poll/download in one piece (a failed task cannot be
    re-polled and its result URL expires); the local WebUI is one blocking
    call. The _novita_* names resolve as module globals at call time so the
    test suite's monkeypatch seam keeps working."""
    if _provider(cfg) == "local":
        return await _local_generate(cfg, image_prompt)
    task_id = await _novita_submit(cfg, image_prompt)
    image_url = await _novita_poll(cfg, task_id)
    return await _download(image_url)


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
    """Reduce a /v3/model response to the model-picker entry shape, dropping
    models that are not deployable (status != 1) or have no usable sd_name.
    `hash` (Novita's truncated SHA256 = Civitai's AutoV2) is what links a
    card to its Civitai page — directly once cached, else via /civitai/page."""
    return [
        {
            "sd_name": m.get("sd_name_in_api") or m.get("sd_name"),
            "name": m.get("name"),
            "is_sdxl": bool(m.get("is_sdxl")),
            "base_model": m.get("base_model"),
            "cover_url": m.get("cover_url"),
            "hash": _hash_prefix10(m.get("hash_sha256")),
            "civitai_url": "",
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


def _hash_prefix10(value) -> str:
    """First 10 hex chars of a hash, lowercased — the AutoV2 form Civitai,
    Novita and A1111 shorthashes share. "" for anything shorter or non-hex
    (6/8-char legacy shorthashes are not AutoV2 and cannot be looked up)."""
    h = str(value or "").strip().lower()
    return h[:10] if re.fullmatch(r"[0-9a-f]{10,}", h) else ""


def _read_ckpt_meta_cache() -> dict:
    """The by-hash lookup cache: {AutoV2 prefix -> {"model_id": int|None,
    "thumb_url": str, "name": str, "checked_at": iso}}. model_id None records
    a checked miss, so unknown files are not re-asked on every request."""
    path = _data_dir() / CIVITAI_CKPT_META_FILE
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[Image Gen] Failed to read {CIVITAI_CKPT_META_FILE}: {e}")
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def _ckpt_meta_stale(entry) -> bool:
    """Hits never expire (Civitai model ids are permanent); misses retry
    after CIVITAI_HASH_MISS_TTL_S."""
    if not isinstance(entry, dict):
        return True
    if entry.get("model_id"):
        return False
    try:
        checked = datetime.fromisoformat(str(entry.get("checked_at") or ""))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - checked).total_seconds() \
        > CIVITAI_HASH_MISS_TTL_S


async def _civitai_version_by_hash(cfg: dict, prefix: str) -> dict | None:
    """One /model-versions/by-hash lookup: {"model_id", "thumb_url", "name"}
    for the model whose file matches the AutoV2 prefix, or None when Civitai
    knows no such file (a real answer — cached as a miss). RuntimeError on
    transport/HTTP trouble (not cached). The thumbnail is the version's
    mildest image (lowest nsfwLevel), routed through Civitai's resizing CDN
    instead of the multi-MB original."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
            resp = await client.get(
                f"{CIVITAI_BASE}/model-versions/by-hash/{prefix}",
                headers=_civitai_headers(cfg))
    except httpx.TransportError as e:
        raise RuntimeError(f"Could not reach Civitai: {e}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"Civitai answered with HTTP {resp.status_code}")
    body = resp.json() or {}
    if not isinstance(body, dict):
        return None
    model_id = body.get("modelId")
    if not model_id:
        return None
    images = [i for i in (body.get("images") or [])
              if isinstance(i, dict) and i.get("url") and i.get("type") != "video"]
    images.sort(key=lambda i: i.get("nsfwLevel") or 0)
    thumb = str(images[0]["url"]) if images else ""
    return {
        "model_id": int(model_id),
        "thumb_url": thumb.replace("/original=true/", "/width=450/"),
        "name": str((body.get("model") or {}).get("name") or ""),
    }


# After a lookup fails on transport, skip further lookups until this
# monotonic instant — /models must stay usable while Civitai is down.
_civitai_hash_backoff = {"until": 0.0}


async def _civitai_hash_meta(cfg: dict, prefixes: list[str],
                             fetch_missing: bool = True) -> dict:
    """Civitai metadata for AutoV2 prefixes: the on-disk cache, plus live
    by-hash lookups for unknown/expired ones (bounded concurrency) when
    fetch_missing — the local picker enriches eagerly, Novita search pages
    read the cache only and resolve links lazily via /civitai/page. Returns
    {prefix: cache entry} for every prefix known afterwards."""
    wanted = [p for p in dict.fromkeys(prefixes) if p]
    cache = _read_ckpt_meta_cache()
    missing = [p for p in wanted if _ckpt_meta_stale(cache.get(p))]
    if missing and fetch_missing \
            and time.monotonic() >= _civitai_hash_backoff["until"]:
        sem = asyncio.Semaphore(CIVITAI_HASH_CONCURRENCY)

        async def _one(prefix: str):
            async with sem:
                return prefix, await _civitai_version_by_hash(cfg, prefix)

        fetched: dict = {}
        for res in await asyncio.gather(*(_one(p) for p in missing),
                                        return_exceptions=True):
            if isinstance(res, BaseException):
                _civitai_hash_backoff["until"] = (
                    time.monotonic() + CIVITAI_HASH_BACKOFF_S)
                print(f"[Image Gen] Civitai hash lookup failed: {res}")
                continue
            prefix, meta = res
            fetched[prefix] = {
                "model_id": (meta or {}).get("model_id"),
                "thumb_url": (meta or {}).get("thumb_url") or "",
                "name": (meta or {}).get("name") or "",
                "checked_at": _now(),
            }
        if fetched:
            # Re-read before merging: a concurrent request may have written
            # other prefixes while these lookups were in flight.
            cache = {**_read_ckpt_meta_cache(), **fetched}
            _atomic_write_json(_data_dir() / CIVITAI_CKPT_META_FILE,
                               {"entries": cache})
    return {p: cache[p] for p in wanted if isinstance(cache.get(p), dict)}


def _apply_civitai_ckpt_meta(models: list[dict], meta: dict) -> None:
    """Fill civitai_url/cover_url on model-picker entries from by-hash
    metadata. An existing cover (Novita's own) is kept — it shows the exact
    mirrored version; the Civitai thumb fills local entries, which have
    none."""
    for m in models:
        entry = meta.get(m.get("hash") or "")
        if not entry or not entry.get("model_id"):
            continue
        m["civitai_url"] = f"https://civitai.com/models/{entry['model_id']}"
        if not m.get("cover_url") and entry.get("thumb_url"):
            m["cover_url"] = entry["thumb_url"]


def _civitai_version_to_entry(model: dict, version: dict,
                              all_hashes: list[str] | None = None) -> dict | None:
    """Reduce one Civitai model version (with its parent model's metadata) to
    the library-entry shape. Returns None for versions without an id."""
    if not version.get("id"):
        return None
    files = version.get("files") or []
    file = next((f for f in files if f.get("primary")), files[0] if files else {})
    sha256 = str((file.get("hashes") or {}).get("SHA256") or "").lower()
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
        "sha256": sha256,
        "all_hashes": all_hashes if all_hashes is not None
        else ([sha256] if sha256 else []),
        "file_name": str(file.get("name") or ""),
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


class SearchOverloadedError(RuntimeError):
    """The upstream search service answered 503 (temporarily overloaded).
    The browse endpoints forward this as HTTP 503 so the UI knows to keep a
    spinner up and retry, instead of showing a terminal error."""


def _flatten_civitai_model(model: dict) -> dict | None:
    """Reduce a Civitai /models hit to the library-entry shape (latest version,
    primary file). Returns None for hits without a downloadable version."""
    versions = model.get("modelVersions") or []
    version = versions[0] if versions else {}
    # Novita may mirror an older version, so keep every version's file hash
    # for availability matching (latest first, like modelVersions).
    all_hashes: list[str] = []
    for v in versions[:10]:
        vfiles = v.get("files") or []
        vfile = next((f for f in vfiles if f.get("primary")), vfiles[0] if vfiles else {})
        vhash = str((vfile.get("hashes") or {}).get("SHA256") or "").lower()
        if vhash and vhash not in all_hashes:
            all_hashes.append(vhash)
    return _civitai_version_to_entry(model, version, all_hashes)


async def _civitai_model_versions(cfg: dict, model_id: int) -> list[dict]:
    """Every downloadable version of one Civitai model in the library-entry
    shape (newest first, as Civitai orders them) — the Install button's
    version picker."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.get(f"{CIVITAI_BASE}/models/{int(model_id)}",
                                    headers=_civitai_headers(cfg))
    except httpx.TransportError as e:
        raise RuntimeError(f"Could not reach Civitai: {e}")
    if resp.status_code == 404:
        raise RuntimeError("Civitai model not found")
    if resp.status_code != 200:
        raise RuntimeError(f"Civitai answered with HTTP {resp.status_code}")
    model = resp.json() or {}
    entries = []
    for version in model.get("modelVersions") or []:
        entry = _civitai_version_to_entry(model, version)
        if entry and entry["download_url"]:
            entries.append(entry)
    return entries


async def _civitai_search_loras(cfg: dict, *, query: str, base_model: str,
                                lora_type: str, sort: str, nsfw_mode: str,
                                cursor: str, limit: int,
                                category: str = "") -> dict:
    return await _civitai_search_models(
        cfg, query=query, base_model=base_model,
        types=lora_type if lora_type in CIVITAI_LORA_TYPES else "LORA",
        sort=sort, nsfw_mode=nsfw_mode, cursor=cursor, limit=limit,
        category=category)


async def _civitai_search_models(cfg: dict, *, query: str, base_model: str,
                                 types: str, sort: str, nsfw_mode: str,
                                 cursor: str, limit: int,
                                 category: str = "") -> dict:
    """One Civitai /models search reduced to the library-entry shape. `types`
    is Civitai's model-type filter — LoRA variants for the LoRA browser,
    "Checkpoint" for the local provider's model browser."""
    import httpx
    if nsfw_mode not in CIVITAI_NSFW_MODES:
        nsfw_mode = "off"
    sort = sort if sort in CIVITAI_SORTS else CIVITAI_SORTS[0]
    # With a `query`, Civitai routes to Meilisearch which ignores `sort` and
    # returns relevance order — so pull several full pages and sort proxy-side.
    pages = CIVITAI_SEARCH_PAGES if query else 1
    fetch_limit = 100 if query else max(1, min(100, limit))
    params = [
        ("types", types),
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
                if resp.status_code == 503:
                    raise SearchOverloadedError(
                        f"Civitai search failed (503): {resp.text[:300]}")
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
        # Word-boundary like everywhere else, so "animagine"-based repos
        # (matched above as sdxl via their own tag spellings or passed
        # through raw) never read as CircleStone's Anima.
        if "animagine" not in base and _ANIMA_WORD_RE.search(base):
            return "Anima"
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
    if resp.status_code == 503:
        raise SearchOverloadedError(
            f"Hugging Face search failed (503): {resp.text[:300]}")
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
        "triggers_llm": False,  # prompt writer picks which trigger words fit each image
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
    if _missing_setup(cfg):
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


def _local_batch_chunks(cells: list[dict], cap: int) -> list[list[int]]:
    """Cell indices grouped into GPU batches. Cells may share a batch only
    when their cfgs yield the exact same <lora:...> tag string (same set AND
    weights): the WebUI activates extra networks batch-wide, so mixing sets
    would apply every image's LoRAs to every image. Groups then split into
    chunks of at most `cap` images (the VRAM ceiling)."""
    cap = max(1, int(cap or 1))
    groups: dict[str, list[int]] = {}
    for i, cell in enumerate(cells):
        groups.setdefault(_local_prompt_lora_tags(cell["cfg"]), []).append(i)
    return [group[j:j + cap]
            for group in groups.values()
            for j in range(0, len(group), cap)]


async def _generate_local_batched(cells: list[dict], cfg: dict, retries,
                                  generate_once) -> list:
    """Render a local multi-image generation as few GPU batches as possible
    via the wb_prompt_batch.py script. Returns (bytes, ext) results or
    exceptions aligned with `cells` -- the exact shape
    asyncio.gather(return_exceptions=True) produces, so the caller's
    partial-failure handling stays shared with the per-image path. A chunk
    that ultimately fails falls back to one request per image for exactly
    its cells: batching can only add speed, never new failure modes."""
    results: list = [None] * len(cells)
    for idxs in _local_batch_chunks(cells, cfg.get("local_batch_size")):
        if len(idxs) > 1:
            group_cfg = cells[idxs[0]]["cfg"]

            async def _batch_call(idxs=idxs, group_cfg=group_cfg):
                prompts = [cells[i]["prompt"] for i in idxs]
                try:
                    return await _local_generate_batch(group_cfg, prompts)
                except NonRetryableError:
                    raise
                except Exception as e:
                    if _local_error_looks_oom(e):
                        # The same batch would just OOM again; skip the
                        # step retries and go straight to the fallback.
                        raise NonRetryableError(str(e)) from e
                    raise

            try:
                for i, res in zip(idxs, await _retry_step(
                        "batched image generation", _batch_call, retries)):
                    results[i] = res
                continue
            except Exception as e:
                print(f"[Image Gen] batched render of {len(idxs)} images "
                      f"failed ({e}); retrying one request per image")
        singles = await asyncio.gather(
            *(_retry_step("image generation",
                          lambda i=i: generate_once(cells[i]), retries)
              for i in idxs),
            return_exceptions=True)
        for i, res in zip(idxs, singles):
            results[i] = res
    return results


async def _generation_pipeline(record_id: str, cfg: dict, narration: str,
                               history: str, sdk, prompt_override: str | None,
                               characters: dict | None = None) -> None:
    started = time.monotonic()
    lock = _get_gen_lock()
    retries = cfg.get("step_retries", STEP_RETRIES_DEFAULT)
    try:
        async with lock:
            image_num = max(1, min(IMAGE_NUM_MAX, int(cfg.get("image_num", 1) or 1)))

            if prompt_override:
                # A verbatim prompt (retry, unrefined studio text) cannot be
                # re-varied; the batch shares it and differs by seed only —
                # one scene-level LoRA gate serves every slot. (No retry
                # here — the gate already fails open on any error.)
                gated = await _apply_lora_conditions(cfg, narration, sdk, characters)
                if gated is not cfg:
                    cfg = gated
                    await _patch_record(record_id, loras=_applied_lora_names(cfg))
                slot_cfgs = [cfg] * image_num
                prompts = [_clean_image_prompt(prompt_override,
                                               cap=_prompt_cap(cfg))] * image_num
            else:
                if sdk is None:
                    raise RuntimeError("no LLM available to write the image prompt")
                await _patch_record(record_id, status="prompting")

                # A batch with no character roster (studio Generate) first
                # fixes the described characters' looks once, so the images
                # agree on every trait; story runs get this from the
                # character reference system instead.
                character_notes = ""
                if image_num > 1 and not characters:
                    character_notes = await _scene_character_notes(cfg, narration, sdk)

                # One shared beat plan next, so every writer sees the same
                # chronology (None falls back to independent splits).
                beats = await _plan_beats(cfg, narration, sdk, image_num)

                # Conditional LoRAs: when each image depicts its own beat,
                # each slot gets its own gate judged against that beat — a
                # 'battle' LoRA can fire on the duel image and stay out of
                # the farewell image — so the trigger words fed to each
                # writer and each slot's submit payload see that slot's set.
                # Without beats one scene-level gate serves the whole batch.
                # (No retry here — the gate already fails open on any error.)
                if beats:
                    slot_cfgs = list(await asyncio.gather(*(
                        _apply_lora_conditions(
                            cfg, _beat_scene(narration, beats[slot]), sdk, characters)
                        for slot in range(image_num))))
                else:
                    gated = await _apply_lora_conditions(cfg, narration, sdk, characters)
                    slot_cfgs = [gated] * image_num
                if any(c is not cfg for c in slot_cfgs):
                    await _patch_record(record_id, loras=_union_lora_names(slot_cfgs))

                # One writer call per image, run concurrently: separate calls
                # (plus a per-slot moment hint) pin each image to its own
                # chronological beat of the scene, so a batch reads as a
                # sequence of what happened rather than N seeds of one
                # prompt. Each slot counts its own attempts so a refusal's
                # retries carry the fiction reminder while first attempts
                # stay unchanged.
                def _writer(slot, hint):
                    attempts = {"n": 0}

                    async def call():
                        attempts["n"] += 1
                        return await _write_image_prompt(
                            slot_cfgs[slot], narration, history, sdk, characters,
                            moment_hint=hint, character_notes=character_notes,
                            insist=attempts["n"] > 1)
                    return call

                prompt_results = await asyncio.gather(
                    *(_retry_step("prompt writing",
                                  _writer(slot, _moment_hint(slot, image_num, beats)),
                                  retries)
                      for slot in range(image_num)),
                    return_exceptions=True)
                survivors = [i for i, p in enumerate(prompt_results)
                             if not isinstance(p, BaseException)]
                if not survivors:
                    raise prompt_results[0]
                # A failed writer slot borrows its NEAREST sibling's prompt
                # (seed still varies) rather than sinking its image or the
                # whole batch -- nearest, so the duplicated beat sits next to
                # its twin and the sequence stays in order.
                prompts = [p if not isinstance(p, BaseException)
                           else prompt_results[min(survivors,
                                                   key=lambda i: (abs(i - slot), i))]
                           for slot, p in enumerate(prompt_results)]

            await _patch_record(record_id, status="generating",
                                image_prompt=prompts[0], image_prompts=prompts)

            # One retryable unit: a failed task cannot be re-polled and the
            # presigned result URL expires, so any failure past submission
            # recovers by resubmitting a fresh task. Each slot's prompt lives
            # in a mutable cell so a content-filter refusal can swap in an
            # LLM-softened rewrite before the next step retry resubmits; the
            # cell also carries the slot's own (per-beat gated) config.
            cells = [{"prompt": p, "cfg": slot_cfgs[i]} for i, p in enumerate(prompts)]

            async def _generate_once(cell):
                try:
                    return await _generate_image(cell["cfg"], cell["prompt"])
                except ProviderRefusal as e:
                    # Verbatim user text is never rewritten behind the user's
                    # back; only LLM-written prompts get softened.
                    if prompt_override is not None or sdk is None:
                        raise
                    cell["prompt"] = await _soften_image_prompt(
                        cell["cfg"], cell["prompt"], str(e), sdk)
                    raise RuntimeError(
                        f"retrying with a softened prompt after refusal: {e}")

            if (_provider(cfg) == "local" and len(cells) > 1
                    and await _local_batch_script_available(cfg)):
                # One request per LoRA-set group renders the whole group as
                # a single GPU batch -- much faster than the WebUI queueing
                # the gathered single-image requests one after another.
                results = await _generate_local_batched(cells, cfg, retries,
                                                        _generate_once)
            else:
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
    if not cfg.get("enabled") or _missing_setup(cfg):
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
    missing = _missing_setup(cfg)
    if missing == "api_key":
        return {"message": "[Image Gen] No API key configured. Add one in Image Studio (main menu).",
                "signal": "end_turn"}
    if missing == "model_name":
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
        scheduler: str | None = None
        hires_enabled: bool | None = None
        hires_scale: float | None = None
        hires_upscaler: str | None = None
        hires_steps: int | None = None
        hires_denoise: float | None = None
        negative_prompt: str | None = None
        interval: int | None = None
        step_retries: int | None = None
        prompt_model_preference: str | None = None
        beat_planner: str | None = None
        prompt_template: str | None = None
        prompt_template_tags: str | None = None
        quality_tags: str | None = None
        pony_quality_tags: str | None = None       # deprecated alias for quality_tags
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
        provider: str | None = None
        local_base_url: str | None = None
        local_auth_user: str | None = None
        local_auth_pass: str | None = None
        local_checkpoint_dir: str | None = None
        local_lora_dir: str | None = None
        local_upscaler_dir: str | None = None
        local_text_encoder_dir: str | None = None
        local_vae_dir: str | None = None
        local_helper_url: str | None = None
        local_helper_token: str | None = None
        local_batch_size: int | None = None

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
        local_name: str | None = None      # manual link to an installed file; "" clears
        condition: str | None = None
        llm_mode: str | None = None
        trained_words: list[str] | None = None
        triggers_llm: bool | None = None

    class InstallRequest(BaseModel):
        # LoRA installs: an already-saved entry (lora_id) or a browse/version-
        # picker item (saved to the library first). Checkpoint installs: a
        # direct URL plus browse metadata (item_id ties the pollable status
        # back to the browse card; base_model feeds "Use as model").
        lora_id: str | None = None
        item: LoraSave | None = None
        kind: str = "lora"                 # a LOCAL_INSTALL_KINDS key
        url: str = ""
        filename: str = ""
        sha256: str = ""
        label: str = ""
        item_id: str = ""
        base_model: str = ""

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
        out["local_auth_pass"] = _mask_key(cfg.get("local_auth_pass", ""))
        out["has_local_auth"] = bool(cfg.get("local_auth_user"))
        out["local_helper_token"] = _mask_key(cfg.get("local_helper_token", ""))
        out["has_helper"] = bool(str(cfg.get("local_helper_url") or "").strip())
        # Whether one-click installs can work per kind: a folder this machine
        # can write to, or an install helper next to a remote WebUI. (A
        # pre-v2 helper has no upscaler folder; its install command then
        # fails with an actionable "update the launcher" message.)
        out["local_install"] = {
            kind: _local_install_dir(cfg, kind) is not None or out["has_helper"]
            for kind in LOCAL_INSTALL_KINDS
        }
        out["providers"] = PROVIDERS
        out["samplers"] = SAMPLERS
        out["default_prompt_template"] = DEFAULT_PROMPT_TEMPLATE
        out["default_prompt_template_tags"] = DEFAULT_PROMPT_TEMPLATE_TAGS
        out["quality_tag_defaults"] = QUALITY_TAG_DEFAULTS
        out["render_defaults"] = RENDER_DEFAULTS
        out["vpred_render_overrides"] = VPRED_RENDER_OVERRIDES
        out["default_negative_prompt"] = DEFAULT_NEGATIVE_PROMPT
        out["default_scheduler"] = DEFAULT_SCHEDULER
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
        local_pass = incoming.pop("local_auth_pass", None)
        if local_pass is not None and not local_pass.startswith(KEY_MASK_PREFIX):
            cfg["local_auth_pass"] = local_pass
        helper_token = incoming.pop("local_helper_token", None)
        if helper_token is not None and not helper_token.startswith(KEY_MASK_PREFIX):
            cfg["local_helper_token"] = helper_token.strip()

        if "provider" in incoming and incoming["provider"] not in PROVIDERS:
            raise HTTPException(status_code=400,
                                detail=f"provider must be one of {PROVIDERS}")
        if "local_base_url" in incoming:
            base = str(incoming["local_base_url"]).strip().rstrip("/")
            if base and not base.startswith(("http://", "https://")):
                raise HTTPException(status_code=400,
                                    detail="local_base_url must start with http:// or https://")
            incoming["local_base_url"] = base or LOCAL_DEFAULT_BASE
        if "local_helper_url" in incoming:
            helper = str(incoming["local_helper_url"]).strip().rstrip("/")
            if helper and not helper.startswith(("http://", "https://")):
                raise HTTPException(status_code=400,
                                    detail="local_helper_url must start with http:// or https://")
            incoming["local_helper_url"] = helper
        for field in ("local_checkpoint_dir", "local_lora_dir",
                      "local_upscaler_dir", "local_text_encoder_dir",
                      "local_vae_dir"):
            if field in incoming:
                # Existence is checked when an install starts, not here — the
                # WebUI may live on another machine or not be mounted yet.
                incoming[field] = os.path.expanduser(str(incoming[field]).strip())
        # The sampler list is Novita's; the local WebUI reports its own via
        # /local/samplers, so any non-empty name is accepted there.
        effective_provider = incoming.get("provider", _provider(cfg))
        if "sampler_name" in incoming:
            if effective_provider == "local":
                name = str(incoming["sampler_name"]).strip()
                if not name or len(name) > 100:
                    raise HTTPException(status_code=400, detail="Invalid sampler name")
                incoming["sampler_name"] = name
            elif incoming["sampler_name"] not in SAMPLERS:
                raise HTTPException(status_code=400, detail=f"Unknown sampler. Allowed: {SAMPLERS}")
        if "scheduler" in incoming:
            # Local-only field; the WebUI defines the valid label set, so any
            # short non-empty string passes (Novita simply never sees it).
            name = str(incoming["scheduler"]).strip()
            if len(name) > 100:
                raise HTTPException(status_code=400, detail="Invalid scheduler name")
            incoming["scheduler"] = name or DEFAULT_SCHEDULER
        if "hires_upscaler" in incoming:
            # Same deal as the scheduler: the WebUI owns the valid name set.
            name = str(incoming["hires_upscaler"]).strip()
            if len(name) > 100:
                raise HTTPException(status_code=400, detail="Invalid upscaler name")
            incoming["hires_upscaler"] = name or DEFAULT_HIRES_UPSCALER
        if "hires_scale" in incoming:
            incoming["hires_scale"] = round(
                max(HIRES_SCALE_MIN, min(HIRES_SCALE_MAX, incoming["hires_scale"])), 2)
        if "hires_steps" in incoming:
            incoming["hires_steps"] = max(0, min(HIRES_STEPS_MAX, incoming["hires_steps"]))
        if "hires_denoise" in incoming:
            incoming["hires_denoise"] = round(
                max(0.0, min(1.0, incoming["hires_denoise"])), 2)
        for side in ("width", "height"):
            if side in incoming:
                incoming[side] = max(128, min(2048, (int(incoming[side]) // 8) * 8))
        if "image_num" in incoming:
            incoming["image_num"] = max(1, min(IMAGE_NUM_MAX, int(incoming["image_num"])))
        if "local_batch_size" in incoming:
            incoming["local_batch_size"] = max(1, min(IMAGE_NUM_MAX,
                                                      int(incoming["local_batch_size"])))
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
        if ("beat_planner" in incoming
                and incoming["beat_planner"] not in BEAT_PLANNER_MODES):
            raise HTTPException(status_code=400,
                                detail=f"beat_planner must be one of {BEAT_PLANNER_MODES}")
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
        # Same for the pre-family-aware quality tags field name.
        legacy_quality = incoming.pop("pony_quality_tags", None)
        if legacy_quality is not None and "quality_tags" not in incoming:
            incoming["quality_tags"] = legacy_quality
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
        q, cur = query.strip(), cursor.strip()
        if _provider(cfg) == "local":
            # The whole installed list ships on the first page: it is dozens
            # of entries, not Novita's thousands, so no cursor is needed.
            try:
                models = await _local_list_checkpoints(cfg)
            except RuntimeError as e:
                raise HTTPException(status_code=502, detail=str(e))
            if q:
                low = q.lower()
                models = [m for m in models
                          if low in m["sd_name"].lower() or low in m["name"].lower()]
            # Civitai previews + page links (the WebUI itself has neither).
            # Titles whose hash the WebUI has not computed yet fall back to
            # the checkpoint folder's scan cache, then the install helper.
            hashless = [m for m in models if not m["hash"]]
            if hashless:
                scan = _read_local_hash_cache(
                    LOCAL_INSTALL_KINDS["checkpoint"]["cache_file"])
                if scan is None or _hash_cache_stale(scan):
                    _spawn_local_hash_scan(cfg, "checkpoint")
                stems = _stem_hash_index(scan)
                for m in hashless:
                    m["hash"] = stems.get(
                        _ckpt_title_stem(m["sd_name"]).casefold(), "")
            if _helper_url(cfg) and any(not m["hash"] for m in models):
                try:
                    indexes = await _helper_hash_indexes(cfg)
                    helper_stems = {stem.casefold(): _hash_prefix10(sha)
                                    for sha, stem in indexes["checkpoint"].items()}
                    for m in models:
                        if not m["hash"]:
                            m["hash"] = helper_stems.get(
                                _ckpt_title_stem(m["sd_name"]).casefold(), "")
                except RuntimeError as e:
                    print(f"[Image Gen] Helper hash index failed: {e}")
            _apply_civitai_ckpt_meta(
                models, await _civitai_hash_meta(cfg, [m["hash"] for m in models]))
            return {"models": models, "next_cursor": "", "effective_query": q}
        if not cfg.get("api_key"):
            raise HTTPException(status_code=400, detail="No API key configured")
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
        # Search pages never wait on Civitai: cards resolve their page link
        # lazily through /civitai/page, and cached hashes get it directly.
        _apply_civitai_ckpt_meta(
            models, await _civitai_hash_meta(cfg, [m["hash"] for m in models],
                                             fetch_missing=False))
        # First-party FLUX.2 rides its own endpoint, so it is not in /v3/model;
        # pin it to the top of matching first pages.
        if not cur and (not q or "flux" in q.lower()):
            models.insert(0, {
                "sd_name": FLUX2_MODEL_NAME,
                "name": "FLUX.2 [dev] — Novita first-party (LoRAs via Civitai link)",
                "is_sdxl": False,
                "base_model": "Flux.2",
                "cover_url": None,
                "hash": "",
                "civitai_url": "",
            })
        return {"models": models, "next_cursor": next_cursor,
                "effective_query": effective_query}

    @router.get("/local/status")
    async def local_status():
        """Connection test for the Image Studio setup card. Always 200 — the
        UI renders {ok: false, error} inline instead of a request failure.
        Probes the install helper too when one is configured."""
        cfg = _load_config()
        out: dict
        try:
            options, models = await asyncio.gather(
                _local_get(cfg, "/sdapi/v1/options"),
                _local_get(cfg, "/sdapi/v1/sd-models"))
        except RuntimeError as e:
            out = {"ok": False, "base_url": _local_base(cfg), "error": str(e)}
        else:
            current = str((options if isinstance(options, dict) else {})
                          .get("sd_model_checkpoint") or "")
            out = {"ok": True, "base_url": _local_base(cfg),
                   "checkpoint_count": len(models if isinstance(models, list) else []),
                   "current_checkpoint": current,
                   # Fresh probe (force): the user testing the connection
                   # right after installing the script deserves the truth,
                   # not a cached "not installed" from before the restart.
                   "batch_script": await _local_batch_script_available(
                       cfg, force=True)}
            if _is_vpred(cfg):
                out["vpred"] = True
                # v-prediction SDXL needs a Forge-family WebUI; Forge builds
                # expose forge_* option keys, classic A1111 does not (and
                # renders v-pred checkpoints as epsilon: dark, blurry,
                # washed-out output).
                if not any(str(k).startswith("forge")
                           for k in (options if isinstance(options, dict) else {})):
                    out["vpred_warning"] = (
                        "This checkpoint is v-prediction but the WebUI does "
                        "not look like Forge — classic AUTOMATIC1111 cannot "
                        "sample SDXL v-pred and renders it as epsilon (dark, "
                        "blurry, washed-out images). Use SD WebUI Forge for "
                        "v-pred checkpoints.")
                diag = await asyncio.to_thread(_vpred_checkpoint_diagnosis, cfg)
                if diag is not None:
                    out["vpred_file_check"] = diag
                    # The missing key is the more specific diagnosis, so it
                    # overwrites the softer WebUI notice.
                    if not diag["has_vpred_key"]:
                        out["vpred_warning"] = (
                            f"{diag['file']} is named v-pred but its "
                            "safetensors header lacks the 'v_pred' key the "
                            "WebUI auto-detects v-prediction from — it will "
                            "be sampled as epsilon (dark, blurry output). "
                            "Re-download the official file from Civitai; "
                            "merges and re-uploads often strip the key.")
            if _checkpoint_family(cfg) == "anima":
                # Anima needs a modules-capable WebUI (Forge Neo) plus the
                # Qwen text encoder + VAE installed; surface both checks
                # here so the Setup card can say exactly what is missing
                # before the first render fails. force=True for the same
                # reason as the batch script above.
                out["anima"] = True
                modules = await _local_list_modules(cfg, force=True)
                if modules is None:
                    out["anima_warning"] = (
                        "This checkpoint is an Anima model but the WebUI "
                        "has no VAE/text-encoder module API — classic "
                        "A1111/Forge cannot run Anima. Use SD WebUI Forge "
                        "Neo (image_server.sh/.bat installs it; existing "
                        "installs migrate with migrate_image_server).")
                else:
                    found = _match_anima_modules(modules)
                    out["anima_modules_found"] = len(found)
                    if len(found) < 2:
                        out["anima_warning"] = (
                            "Anima needs its text encoder "
                            f"({ANIMA_TEXT_ENCODER_FILE}) and VAE "
                            f"({ANIMA_VAE_FILE}) next to the WebUI — "
                            "install both with one click below, or drop "
                            "them into models/text_encoder and models/VAE.")
        if _helper_url(cfg):
            try:
                health = await _helper_request(cfg, "GET", "/wb-helper/health",
                                               timeout=10.0)
                out["helper"] = {"ok": bool(health.get("ok")),
                                 "kinds": health.get("kinds") or {}}
            except RuntimeError as e:
                out["helper"] = {"ok": False, "error": str(e)}
        else:
            detected = await _detect_helper(cfg)
            if detected:
                out["helper_detected"] = detected
        return out

    @router.get("/helper-script")
    async def helper_script():
        """helper_server.py as a download, for WebUI machines that don't have
        this repo — copy the one file over and run it with any Python 3."""
        from fastapi.responses import FileResponse
        path = Path(__file__).resolve().parent / "helper_server.py"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="helper_server.py not found")
        return FileResponse(
            path, media_type="text/x-python",
            headers={"Content-Disposition": 'attachment; filename="helper_server.py"',
                     "Cache-Control": "no-cache"})

    @router.get("/local/loras")
    async def local_loras():
        """Installed LoRAs as the WebUI names them, for the manual link picker.
        The alias (kohya's ss_output_name) also works in prompt syntax."""
        cfg = _load_config()
        try:
            body = await _local_get(cfg, "/sdapi/v1/loras")
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        loras = []
        for item in body if isinstance(body, list) else []:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            loras.append({"name": str(item["name"]).strip(),
                          "alias": str(item.get("alias") or "").strip(),
                          "path": str(item.get("path") or "")})
        return {"loras": loras}

    @router.post("/local/match-loras")
    async def local_match_loras():
        """Hash-scan the configured LoRA folder and (re)link non-manual
        library entries to installed files by SHA256. Files that vanished
        unlink, so the badge never claims a deleted LoRA still applies."""
        cfg = _load_config()
        lora_dir = str(cfg.get("local_lora_dir") or "").strip()
        if lora_dir and Path(lora_dir).is_dir():
            async with _get_local_scan_lock():
                cache = await _scan_local_hashes(lora_dir)
            index = _local_hash_index(cache) or {}
            file_count = len(cache.get("files", {}))
        elif _helper_url(cfg):
            # WebUI on another machine: the helper's hash index stands in for
            # the folder scan.
            try:
                indexes = await _helper_hash_indexes(cfg)
            except RuntimeError as e:
                raise HTTPException(status_code=502, detail=str(e))
            if indexes["scanning"]:
                raise HTTPException(status_code=503,
                                    detail="The install helper is still hashing "
                                           "its folders — try again in a moment")
            index = indexes["lora"]
            file_count = len(index)
        elif not lora_dir:
            raise HTTPException(status_code=400,
                                detail="Set your WebUI's LoRA folder in Setup "
                                       "first (or the install helper URL for a "
                                       "remote WebUI)")
        else:
            raise HTTPException(status_code=400,
                                detail=f"LoRA folder not found: {lora_dir}")

        cfg = _load_config()  # the scan awaited; re-load before mutating
        now = _now()
        checked = matched = 0
        for entry in cfg.get("lora_library") or []:
            if not isinstance(entry, dict):
                continue
            if str((entry.get("local") or {}).get("source") or "") == "manual":
                continue  # user-made links outrank the scan
            if not _entry_hashes(entry):
                continue
            checked += 1
            stem = _match_local_hashes(index, entry)
            if stem:
                entry["local"] = {"name": stem, "source": "hash"}
                matched += 1
            else:
                entry.pop("local", None)
            entry["local_checked_at"] = now
        _save_config(cfg)
        return {"lora_library": cfg["lora_library"], "matched": matched,
                "checked": checked, "files": file_count}

    def _public_download(status: dict) -> dict:
        # The raw URL may carry the user's Civitai token — never expose it.
        return {k: v for k, v in status.items() if k != "url"}

    @router.post("/local/downloads")
    async def start_install(req: InstallRequest):
        """One-click install: stream a Civitai/HF file into the WebUI's model
        folder, then refresh the WebUI and link the library entry. Returns a
        pollable status; GET /local/downloads tracks progress."""
        cfg = _load_config()
        kind = req.kind if req.kind in LOCAL_INSTALL_KINDS else "lora"
        spec = LOCAL_INSTALL_KINDS[kind]
        refresh_path = spec["refresh_path"]
        # No folder this machine can write to + a configured helper = the
        # WebUI runs on another machine; the helper downloads the file there.
        dest_dir = _local_install_dir(cfg, kind)
        remote = dest_dir is None and bool(_helper_url(cfg))
        if dest_dir is None and not remote:
            dest = str(cfg.get(spec["dir_key"]) or "").strip()
            if not dest:
                raise HTTPException(
                    status_code=400,
                    detail=f"Set your WebUI's {spec['label']} folder in Setup "
                           "first (or the install helper URL for a remote WebUI)")
            raise HTTPException(status_code=400,
                                detail=f"{spec['label']} folder not found: {dest}")

        lora_id = (req.lora_id or "").strip() or None
        item_id = (req.item_id or "").strip() or None
        label = (req.label or "").strip()
        if kind == "lora":
            if req.item is not None:
                entry = await _intake_lora(req.item)
                lora_id = str(entry.get("id"))
                cfg = _load_config()   # the intake may have written config
            elif lora_id:
                entry = _find_lora(cfg, lora_id)
            else:
                raise HTTPException(status_code=400,
                                    detail="Pass lora_id or item to install a LoRA")
            if entry.get("gated"):
                raise HTTPException(status_code=400,
                                    detail="Gated Hugging Face repos can't be "
                                           "downloaded automatically")
            url = _lora_download_link(entry, cfg)
            if not url:
                raise HTTPException(status_code=404,
                                    detail="No download link for this LoRA")
            version = str(entry.get("version_name") or "").strip()
            label = label or " — ".join(
                p for p in (str(entry.get("name") or "LoRA"), version) if p)
            fallback_name = str(entry.get("name") or "lora")
            expected_hashes = _entry_hashes(entry)
        else:
            url = (req.url or "").strip()
            if not url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=400,
                    detail=f"{spec['label'].capitalize()} installs need an http(s) URL")
            if "civitai.com" in url:
                key = str(cfg.get("civitai_api_key") or "").strip()
                if key:
                    url += ("&" if "?" in url else "?") + "token=" + key
            fallback_name = (req.filename or "").strip() or label or spec["label"]
            label = label or fallback_name
            sha = (req.sha256 or "").strip().lower()
            expected_hashes = [sha] if sha else []
            lora_id = None

        if remote:
            # The helper streams the file on the WebUI's machine and tracks
            # byte progress in the same status shape; the merged
            # GET /local/downloads list feeds the UI's progress bars either
            # way. It dedupes in-flight commands itself.
            try:
                body = await _helper_request(
                    cfg, "POST", "/wb-helper/downloads", json_body={
                        "kind": kind, "url": url, "filename": fallback_name,
                        "label": label, "expected_hashes": expected_hashes,
                        "lora_id": lora_id, "item_id": item_id,
                        "base_model": (req.base_model or "").strip(),
                    })
            except RuntimeError as e:
                raise HTTPException(status_code=502, detail=str(e))
            download = body.get("download") if isinstance(body.get("download"), dict) else {}
            download["remote"] = True
            return {"download": download}

        # The derived upscaler folder (models/ESRGAN) may not exist on a
        # fresh WebUI; creating it is exactly what a manual install would do.
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(status_code=400,
                                detail=f"Cannot create {dest_dir}: {e}")

        # A second click while the same file is in flight returns the
        # existing download instead of racing it.
        for d in _downloads.values():
            if d["status"] == "downloading" and (
                    (lora_id and d.get("lora_id") == lora_id)
                    or (item_id and d.get("item_id") == item_id)
                    or d.get("url") == url):
                return {"download": _public_download(d)}

        dl_id = uuid.uuid4().hex[:12]
        _downloads[dl_id] = {
            "id": dl_id, "kind": kind, "label": label, "filename": "",
            "dest_dir": str(dest_dir), "url": url,
            "total_bytes": 0, "received_bytes": 0,
            "status": "downloading", "error": None, "lora_id": lora_id,
            "item_id": item_id, "base_model": (req.base_model or "").strip(),
            "started_at": _now(), "completed_at": None,
        }
        task = asyncio.get_running_loop().create_task(
            _download_file_pipeline(dl_id, url, dest_dir, fallback_name,
                                    expected_hashes, refresh_path, lora_id, cfg,
                                    kind=kind))
        _download_tasks[dl_id] = task
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return {"download": _public_download(_downloads[dl_id])}

    @router.get("/local/downloads")
    async def list_installs():
        """Local downloads plus the install helper's, merged into one
        pollable list — remote entries carry the same byte-progress fields,
        so the UI's download bars work for both. A helper download flipping
        to done triggers the WebUI refresh / library link exactly once."""
        cfg = _load_config()
        items = sorted(_downloads.values(),
                       key=lambda d: str(d.get("started_at") or ""), reverse=True)
        merged = [_public_download(d) for d in items]
        if _helper_url(cfg):
            try:
                body = await _helper_request(cfg, "GET", "/wb-helper/downloads",
                                             timeout=10.0)
                remote = [d for d in (body.get("downloads") or [])
                          if isinstance(d, dict) and d.get("id")]
            except RuntimeError as e:
                print(f"[Image Gen] Helper download poll failed: {e}")
                remote = []
            for d in remote:
                d["remote"] = True
                if d.get("status") == "done" and d["id"] not in _remote_done_seen:
                    _remote_done_seen.add(d["id"])
                    _spawn_remote_install_followup(cfg, d)
            merged = remote + merged
        return {"downloads": merged}

    @router.delete("/local/downloads/{dl_id}")
    async def cancel_install(dl_id: str):
        status = _downloads.get(dl_id)
        if status is None:
            cfg = _load_config()
            if _helper_url(cfg):
                try:
                    body = await _helper_request(
                        cfg, "DELETE", f"/wb-helper/downloads/{dl_id}")
                except RuntimeError as e:
                    raise HTTPException(status_code=502, detail=str(e))
                download = body.get("download") if isinstance(body.get("download"), dict) else {}
                download["remote"] = True
                return {"download": download}
            raise HTTPException(status_code=404, detail="No such download")
        task = _download_tasks.get(dl_id)
        if status["status"] == "downloading" and task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return {"download": _public_download(_downloads[dl_id])}

    @router.get("/local/samplers")
    async def local_samplers():
        """The WebUI's own sampler list; the static Novita list on any error
        so the select never renders empty."""
        cfg = _load_config()
        try:
            body = await _local_get(cfg, "/sdapi/v1/samplers")
            names = [str(s.get("name")) for s in (body if isinstance(body, list) else [])
                     if isinstance(s, dict) and s.get("name")]
        except RuntimeError:
            names = []
        return {"samplers": names or list(SAMPLERS)}

    @router.get("/local/upscaler-catalog")
    async def local_upscaler_catalog():
        """The curated one-click upscaler list with per-entry install state.
        "installed" is file presence in the upscaler folder (or, for a
        remote WebUI, a SHA256 hit in the install helper's index) — immediate
        and restart-independent; whether the WebUI has LOADED the file is the
        /local/upscalers dropdown's business (new files appear there only
        after a WebUI restart, since no upscaler rescan route exists)."""
        cfg = _load_config()
        root = _local_install_dir(cfg, "upscaler")
        present: set[str] = set()
        if root is not None and root.is_dir():
            present = {p.name.lower() for p in root.rglob("*") if p.is_file()}
        helper_shas: set[str] = set()
        if root is None and _helper_url(cfg):
            try:
                helper_shas = set((await _helper_hash_indexes(cfg))
                                  .get("upscaler") or {})
            except RuntimeError as e:
                print(f"[Image Gen] Helper hash index failed: {e}")
        entries = [{**entry,
                    "installed": entry["filename"].lower() in present
                                 or entry["sha256"] in helper_shas}
                   for entry in UPSCALER_CATALOG]
        return {"entries": entries,
                "can_install": root is not None or bool(_helper_url(cfg)),
                "dir": str(root) if root is not None else None}

    @router.get("/local/anima-catalog")
    async def local_anima_catalog():
        """Anima's Qwen text encoder + VAE with per-entry install state, the
        upscaler catalog's twin across two folder kinds. "installed" is file
        presence in the kind's folder (or a SHA256 hit in the install
        helper's index); whether the WebUI has picked the files up is
        /local/status's anima_modules_found."""
        cfg = _load_config()
        helper_indexes: dict | None = None
        entries = []
        can_install = bool(_helper_url(cfg))
        for entry in ANIMA_MODULE_CATALOG:
            kind = entry["kind"]
            root = _local_install_dir(cfg, kind)
            can_install = can_install or root is not None
            installed = False
            if root is not None and root.is_dir():
                present = {p.name.lower() for p in root.rglob("*") if p.is_file()}
                installed = entry["filename"].lower() in present
            elif _helper_url(cfg):
                if helper_indexes is None:
                    try:
                        helper_indexes = await _helper_hash_indexes(cfg)
                    except RuntimeError as e:
                        print(f"[Image Gen] Helper hash index failed: {e}")
                        helper_indexes = {}
                installed = entry["sha256"] in (helper_indexes.get(kind) or {})
            entries.append({**entry, "installed": installed,
                            "dir": str(root) if root is not None else None})
        return {"entries": entries, "can_install": can_install}

    @router.get("/local/upscalers")
    async def local_upscalers():
        """Upscaler names for the hires-fix dropdown: the WebUI's latent
        modes (which /sdapi/v1/upscalers omits) plus its upscaler models,
        or the static fallback when the WebUI can't answer. "None" is
        dropped — it means "no upscaling" and defeats the feature."""
        cfg = _load_config()
        names: list[str] = []
        for path in ("/sdapi/v1/latent-upscale-modes", "/sdapi/v1/upscalers"):
            try:
                body = await _local_get(cfg, path)
            except RuntimeError:
                continue
            for item in body if isinstance(body, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if name and name.lower() != "none" and name not in names:
                    names.append(name)
        return {"upscalers": names or list(UPSCALERS)}

    @router.get("/local/schedulers")
    async def local_schedulers():
        """The WebUI's scheduler labels plus whether the WebUI supports the
        scheduler API at all (pre-1.9 A1111 doesn't; the payload field is
        withheld there). The static list keeps the select usable when the
        WebUI is merely unreachable. force=True for the same reason as
        /local/status: a settings screen probe deserves fresh truth."""
        cfg = _load_config()
        labels = await _local_list_schedulers(cfg, force=True)
        return {"schedulers": labels or list(SCHEDULERS),
                "supported": labels is not None}

    @router.post("/local/refresh")
    async def local_refresh():
        """Make the WebUI rescan its model folders, then report status."""
        cfg = _load_config()
        errors = []
        for path in ("/sdapi/v1/refresh-checkpoints", "/sdapi/v1/refresh-loras"):
            try:
                await _local_post(cfg, path, timeout=60.0)
            except RuntimeError as e:
                errors.append(str(e))
        status = await local_status()
        if errors and status.get("ok"):
            status = {**status, "ok": False, "error": "; ".join(errors)}
        return status

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
        except SearchOverloadedError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        if _provider(cfg) == "local":
            await _annotate_browse_availability(cfg, result["items"], "lora")
        else:
            _annotate_novita_availability(cfg, result["items"])
        return result

    @router.get("/civitai/checkpoints")
    async def civitai_checkpoints(query: str = "", base_model: str = "",
                                  sort: str = "Most Downloaded", nsfw: str = "off",
                                  category: str = "", cursor: str = "",
                                  limit: int = 24):
        """Civitai checkpoint browsing for the local provider's model browser:
        the same proxy shape as /civitai/loras, with results badged against a
        hash scan of the WebUI's checkpoint folder."""
        cfg = _load_config()
        if nsfw not in CIVITAI_NSFW_MODES:
            nsfw = "off"
        if nsfw != "off" and not cfg.get("civitai_api_key"):
            raise HTTPException(status_code=400,
                                detail="NSFW browsing needs a Civitai API key")
        try:
            result = await _civitai_search_models(
                cfg, query=query.strip(), base_model=base_model.strip(),
                types="Checkpoint", sort=sort, nsfw_mode=nsfw,
                category=category.strip().lower(),
                cursor=cursor.strip(), limit=limit)
        except SearchOverloadedError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        if _provider(cfg) == "local":
            await _annotate_browse_availability(cfg, result["items"], "checkpoint")
        return result

    @router.get("/civitai/page")
    async def civitai_page(hash: str = "", name: str = ""):
        """Redirect to a model's Civitai page (the .red domain, like every
        Civitai link in the UI), resolved from its AutoV2 file hash through
        the by-hash cache. Model-picker cards use this as their image link
        when the page is not known at render time (Novita search results), so
        clicking costs at most one lookup instead of one per search result."""
        cfg = _load_config()
        prefix = _hash_prefix10(hash)
        if not prefix:
            raise HTTPException(status_code=404,
                                detail="This model carries no usable file hash")
        meta = (await _civitai_hash_meta(cfg, [prefix])).get(prefix)
        if meta is None:   # lookup failed (unreachable/backoff) — not a miss
            raise HTTPException(status_code=502,
                                detail="Civitai could not be reached — try again")
        if not meta.get("model_id"):
            label = name.strip() or prefix
            raise HTTPException(
                status_code=404,
                detail=f'"{label}" has no matching Civitai model page')
        return RedirectResponse(f"https://civitai.red/models/{meta['model_id']}")

    @router.get("/civitai/model-versions/{model_id}")
    async def civitai_model_versions(model_id: int):
        """Every downloadable version of a Civitai model, for the Install
        button's version picker (browse hits carry only the latest)."""
        cfg = _load_config()
        try:
            versions = await _civitai_model_versions(cfg, model_id)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        if _provider(cfg) == "local":
            # Checkpoints and LoRAs install into different folders, so each
            # version badges against its own type's matching sources.
            by_kind: dict[str, list[dict]] = {"lora": [], "checkpoint": []}
            for v in versions:
                is_ckpt = str(v.get("type") or "").lower() == "checkpoint"
                by_kind["checkpoint" if is_ckpt else "lora"].append(v)
            for kind, group in by_kind.items():
                if group:
                    await _annotate_browse_availability(cfg, group, kind)
        return {"versions": versions}

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
        except SearchOverloadedError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        if _provider(cfg) == "local":
            await _annotate_browse_availability(cfg, result["items"], "lora")
        else:
            _annotate_novita_availability(cfg, result["items"])
        return result

    def _find_lora(cfg: dict, lora_id: str) -> dict:
        entry = next((e for e in cfg.get("lora_library") or []
                      if isinstance(e, dict) and e.get("id") == lora_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="LoRA not in library")
        return entry

    async def _intake_lora(item: LoraSave) -> dict:
        """Normalize a browse item, match availability, and append it to the
        library. An id already in the library returns its existing entry, so
        the Install flow can reuse this without save_lora's 409."""
        cfg = _load_config()
        library = cfg.get("lora_library") or []
        existing = next((e for e in library
                         if isinstance(e, dict) and e.get("id") == item.id), None)
        if existing is not None:
            return existing
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
        # A file already sitting in the local LoRA folder links immediately.
        index = _local_hash_index(_read_local_hash_cache())
        if index:
            stem = _match_local_hashes(index, entry)
            if stem:
                entry["local"] = {"name": stem, "source": "hash"}
                entry["local_checked_at"] = _now()

        cfg = _load_config()  # re-load: the match awaited, config may have moved
        library = cfg.get("lora_library") or []
        if not any(isinstance(e, dict) and e.get("id") == entry["id"] for e in library):
            library.append(entry)
        cfg["lora_library"] = library
        _save_config(cfg)
        return entry

    @router.post("/loras")
    async def save_lora(item: LoraSave):
        cfg = _load_config()
        if any(isinstance(e, dict) and e.get("id") == item.id
               for e in cfg.get("lora_library") or []):
            raise HTTPException(status_code=409, detail="Already in library")
        entry = await _intake_lora(item)
        return {"entry": entry, "lora_library": _load_config()["lora_library"]}

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
        if patch.local_name is not None:
            name = patch.local_name.strip()
            if name:
                entry["local"] = {"name": name, "source": "manual"}
            else:
                entry.pop("local", None)
            entry["local_checked_at"] = _now()
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
        if patch.triggers_llm is not None:
            entry["triggers_llm"] = bool(patch.triggers_llm)
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
        missing = _missing_setup(cfg)
        if missing == "api_key":
            raise HTTPException(status_code=400, detail="No API key configured")
        if missing == "model_name":
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
