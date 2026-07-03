"""Inventory & Economy -- player items, currency, and consistency enforcement."""
import json


DEFAULT_STARTING_ITEMS = [
    {"name": "Travel Clothes", "qty": 1, "description": "Sturdy, well-worn traveling outfit", "tags": ["clothing"]},
    {"name": "Waterskin", "qty": 1, "description": "Leather waterskin, full", "tags": ["supplies"]},
    {"name": "Rations", "qty": 3, "description": "A day's worth of dried food", "tags": ["supplies", "food"]},
    {"name": "Knife", "qty": 1, "description": "Simple utility knife", "tags": ["tool", "weapon"]},
]

MAX_RENDERED_ITEMS = 30
MAX_DESCRIPTION_LEN = 120

# Cheap pre-gate: only run the LLM enforcement check when the narration even
# mentions money changing hands or equipment-like usage.
TRANSACTION_KEYWORDS = [
    "pay", "pays", "paid", "payment", "buy", "buys", "bought", "purchase",
    "spend", "spends", "spent", "afford", "price", "cost", "coin", "coins",
    "hand over", "hands over", "sold", "sells",
]


def _config(state: dict) -> dict:
    return state.get("module_configs", {}).get("wb_inventory", {})


def _data(state: dict):
    return state.get("module_data", {}).get("wb_inventory")


def _default_inventory(config: dict) -> dict:
    try:
        currency = max(0, int(config.get("starting_currency", 25)))
    except (TypeError, ValueError):
        currency = 25
    return {
        "items": [dict(item) for item in DEFAULT_STARTING_ITEMS],
        "currency": currency,
    }


def _merge_item(items: list, name: str, qty: int, description: str = "", tags=None) -> None:
    """Add qty of an item, merging case-insensitively with an existing entry."""
    key = name.strip().lower()
    for entry in items:
        if entry.get("name", "").strip().lower() == key:
            entry["qty"] = entry.get("qty", 0) + qty
            if description and not entry.get("description"):
                entry["description"] = description
            return
    entry = {"name": name.strip(), "qty": qty}
    if description:
        entry["description"] = description
    if tags:
        entry["tags"] = tags
    items.append(entry)


def _remove_item(items: list, name: str, qty: int) -> bool:
    """Decrement qty of an item case-insensitively; drop the entry at zero.
    Returns True if anything changed. Unknown names are ignored."""
    key = name.strip().lower()
    for entry in items:
        if entry.get("name", "").strip().lower() == key:
            entry["qty"] = max(0, entry.get("qty", 0) - qty)
            if entry["qty"] == 0:
                items.remove(entry)
            return True
    return False


def _item_summary(data: dict, limit: int = MAX_RENDERED_ITEMS) -> str:
    items = data.get("items", [])
    if not items:
        return "(nothing)"
    parts = []
    for entry in items[:limit]:
        label = f"{entry.get('name', '?')} x{entry.get('qty', 1)}"
        if entry.get("description"):
            label += f" ({entry['description']})"
        parts.append(label)
    if len(items) > limit:
        parts.append(f"...and {len(items) - limit} more minor items")
    return ", ".join(parts)


def _parse_json_block(raw: str):
    """Strip Markdown code fences and parse a JSON object/array from an LLM reply."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def on_gather_context(state: dict, sdk) -> dict:
    """Lazy-seed defaults so legacy saves and mid-save toggle-on get an inventory."""
    data = _data(state)
    if not isinstance(data, dict) or "items" not in data:
        return {"module_data": {"wb_inventory": _default_inventory(_config(state))}}
    return {}


async def on_render_prompt_block(block: dict, state: dict, sdk) -> dict:
    if block.get("id") != "inventory_context":
        return {}
    data = _data(state)
    if not isinstance(data, dict):
        return {}
    config = _config(state)
    currency_name = config.get("currency_name", "gold") or "gold"
    lines = [
        "<inventory>",
        f"Carried: {_item_summary(data)}",
        f"Money: {data.get('currency', 0)} {currency_name}",
        "The player can only use items listed here and only spend money they have. "
        "If they acquire or lose items or money in the scene, narrate it explicitly.",
        "</inventory>",
    ]
    return {"content": "\n".join(lines)}


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict:
    data = _data(state)
    if not isinstance(data, dict) or "items" not in data:
        data = _default_inventory(_config(state))
    else:
        data = {"items": [dict(e) for e in data.get("items", [])],
                "currency": data.get("currency", 0)}
    if not mutation:
        return {}

    changed = False

    for entry in mutation.get("items_gained", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        try:
            qty = max(1, int(entry.get("qty", 1)))
        except (TypeError, ValueError):
            qty = 1
        description = str(entry.get("description", "") or "")[:MAX_DESCRIPTION_LEN]
        tags = [str(t) for t in entry.get("tags", []) if t] if isinstance(entry.get("tags"), list) else None
        _merge_item(data["items"], name, qty, description, tags)
        changed = True

    for entry in mutation.get("items_lost", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        try:
            qty = max(1, int(entry.get("qty", 1)))
        except (TypeError, ValueError):
            qty = 1
        if _remove_item(data["items"], name, qty):
            changed = True

    try:
        delta = int(mutation.get("currency_change", 0) or 0)
    except (TypeError, ValueError):
        delta = 0
    if delta != 0:
        data["currency"] = max(0, data.get("currency", 0) + delta)
        changed = True

    if changed:
        return {"module_data": {"wb_inventory": data}}
    return {}


async def on_validate_output(llm_output: str, state: dict, sdk) -> None:
    """Veto narration where the player spends money or uses significant items
    they don't have. Layered gates keep this off the hot path and guarantee at
    most ONE rewrite per turn from this module (wb_core_rpg's veto had to be
    disabled over a rewrite loop -- don't repeat that)."""
    config = _config(state)
    if not config.get("enforcement_enabled", True):
        return
    data = _data(state)
    if not isinstance(data, dict) or "items" not in data:
        return
    if state.get("veto_retries", 0) >= 1:
        return

    currency_name = (config.get("currency_name", "gold") or "gold").lower()
    text_lower = llm_output.lower()
    if not any(kw in text_lower for kw in TRANSACTION_KEYWORDS + [currency_name]):
        return

    prompt = f"""You are a consistency checker for a text RPG. Output ONLY valid JSON.

The player's actual possessions:
  Items: {_item_summary(data)}
  Money: {data.get('currency', 0)} {currency_name}

Narration to check:
\"\"\"{llm_output[:3000]}\"\"\"

Only report a violation if the narration clearly shows the PLAYER spending more money than they have, or wielding/consuming a significant item that is plainly absent from their possessions. Improvised or ambient objects (a rock, a chair, furniture), items acquired earlier in this same narration, and other characters' possessions are NOT violations. When unsure, report no violation.

JSON response:
{{"violation": bool, "kind": "currency"|"item"|"", "detail": "one short sentence, empty if no violation"}}"""

    try:
        raw = await sdk.llm.generate(prompt, model_preference=config.get("enforcement_ai_model", "fastest"))
    except Exception:
        return

    parsed = _parse_json_block(raw)
    if not isinstance(parsed, dict) or not parsed.get("violation"):
        return

    detail = str(parsed.get("detail", "")) or "the player used money or items they do not possess"
    raise sdk.ValidationVeto(
        f"Inventory violation: {detail} "
        f"The player actually carries: {_item_summary(data)}; money: {data.get('currency', 0)} {currency_name}. "
        f"Rewrite so the player only uses what they actually possess "
        f"(or have the acquisition happen on-screen first)."
    )


async def on_character_get_defaults(state: dict, world_context: dict) -> dict:
    return _default_inventory({})


async def on_command_inventory(args: list, state: dict, sdk):
    data = _data(state)
    if not isinstance(data, dict):
        return {"message": "[Inventory] Empty.", "signal": "end_turn"}
    config = _config(state)
    currency_name = config.get("currency_name", "gold") or "gold"
    lines = [f"[Inventory] {data.get('currency', 0)} {currency_name}"]
    items = data.get("items", [])
    if items:
        for entry in items:
            line = f"  {entry.get('name', '?')} x{entry.get('qty', 1)}"
            if entry.get("description"):
                line += f" — {entry['description']}"
            lines.append(line)
    else:
        lines.append("  (nothing carried)")
    return {"message": "\n".join(lines), "signal": "end_turn"}
