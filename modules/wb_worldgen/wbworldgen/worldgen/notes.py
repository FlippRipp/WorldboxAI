"""Ideation notes (C5 of the worldgen plan): the brief's third artifact.

A note is an established fact or decision from the ideation conversation
that is neither seed direction nor a world rule: ``{"id", "text",
"subject"}``, where an empty subject means world-scoped and a named subject
("the sand planet Kharos") scopes the note to the thing it names (N1).
Subjects bind per-map against the compiled world — the map whose label/id
the subject names, or the map containing a named node it names — via the
same ``join_key`` normalization every cross-step name join uses (N2).
Binding is recomputed whenever it is needed, never persisted as build-time
truth: maps appear and rename mid-build.

Amendment state from the verifier's compromise channel (N5/N7) rides the
note dict: ``status`` ("amended" after a compromise), ``original_text``
(the pre-compromise text, restored on veto), ``rationale`` (why), and
``no_compromise`` (True once the user vetoed — the note can never be
amended again).

Pure functions over plain dicts (no I/O, no services) so every consumer —
the ideation route, the Go handoff, the injection seams, the lints and the
note verifier — shares one implementation. Functions that read the brief
take any dict carrying a ``"brief"`` key: the world state and the compiled
world (the compiler copies the brief through) both qualify.
"""

from wbworldgen.mapmodel import join_key
from wbworldgen.worldgen import mapspace as _ms

#: Containment matches shorter than this many normalized characters are
#: ignored — tiny overlaps would bind everything to everything.
_MIN_CONTAINMENT = 4

#: Amendment-state keys preserved through cleaning (see module docstring).
_CARRIED_KEYS = ("id", "status", "original_text", "rationale", "no_compromise")


def clean_notes(raw) -> list:
    """Sanitized copy of a notes draft (client, LLM or brief): dicts with a
    non-empty ``text``; ``subject`` trimmed, empty = world-scoped. A bare
    string is accepted as a world-scoped note (models sometimes flatten
    single-field objects). Amendment-state keys are carried through; ids are
    kept when present (``assign_ids`` at the Go handoff fills gaps)."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            text, subject, extra = item.strip(), "", {}
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            subject = str(item.get("subject") or "").strip()
            extra = item
        else:
            continue
        if not text:
            continue
        note = {"text": text, "subject": subject}
        for key in _CARRIED_KEYS:
            if extra.get(key):
                note[key] = extra[key]
        out.append(note)
    return out


def assign_ids(notes: list) -> list:
    """Stable ids (``n1``..``nk``) for the Go handoff (N1). Existing ids
    survive; new notes fill the first free slots."""
    used = {n.get("id") for n in notes if n.get("id")}
    out, i = [], 1
    for n in notes:
        n = dict(n)
        if not n.get("id"):
            while f"n{i}" in used:
                i += 1
            n["id"] = f"n{i}"
            used.add(n["id"])
        out.append(n)
    return out


def brief_notes(state: dict) -> list:
    """The brief's notes, cleaned; ``[]`` without a brief. (The
    ``world_rules.brief_rules`` precedent, for notes.)"""
    brief = (state or {}).get("brief")
    if not isinstance(brief, dict):
        return []
    return clean_notes(brief.get("notes"))


def world_note_texts(state: dict) -> list:
    """Texts of the world-scoped notes (the seed-seam payload, N3)."""
    return [n["text"] for n in brief_notes(state) if not n.get("subject")]


def subject_notes(state: dict) -> list:
    """The notes that carry a subject, cleaned."""
    return [n for n in brief_notes(state) if n.get("subject")]


# --- binding (N2) ------------------------------------------------------------

def _subject_matches_name(subject_key: str, name) -> bool:
    """join_key containment either way, length-guarded on the contained
    side."""
    nk = join_key(name)
    if not nk or not subject_key:
        return False
    if nk == subject_key:
        return True
    if len(nk) >= _MIN_CONTAINMENT and nk in subject_key:
        return True
    return len(subject_key) >= _MIN_CONTAINMENT and subject_key in nk


def bind_subject(subject: str, compiled: dict) -> tuple:
    """The map a subject names: ``(map_id, [])`` on a unique match, ``(None,
    candidates)`` when several maps match in the same tier (ambiguous), and
    ``(None, [])`` when nothing matches. Tiers, strongest first: exact
    ``join_key`` on a map's label/id; exact on a named node's name (its
    map); containment on map labels; containment on node names."""
    key = join_key(subject)
    if not key:
        return None, []
    maps = _ms.maps_by_id(compiled)

    exact_maps, exact_nodes, contain_maps, contain_nodes = set(), set(), set(), set()
    for mid, rec in maps.items():
        label_key = join_key(rec.get("label"))
        if key in (label_key, join_key(mid)):
            exact_maps.add(mid)
        elif label_key and _subject_matches_name(key, rec.get("label")):
            contain_maps.add(mid)
        for n in rec.get("nodes", []):
            if not n.get("name"):
                continue
            if join_key(n["name"]) == key:
                exact_nodes.add(mid)
            elif _subject_matches_name(key, n["name"]):
                contain_nodes.add(mid)

    for tier in (exact_maps, exact_nodes, contain_maps, contain_nodes):
        if len(tier) == 1:
            return next(iter(tier)), []
        if len(tier) > 1:
            return None, sorted(tier)
    return None, []


def bound_notes(state: dict, compiled: dict) -> list:
    """Every brief note with its live binding: adds ``scope`` (``"world"`` |
    ``"map"`` | ``"unbound"``), ``map_id`` (map scope) and ``candidates``
    (the ambiguous matches, unbound only)."""
    out = []
    for n in brief_notes(state):
        n = dict(n)
        if not n.get("subject"):
            n["scope"] = "world"
        else:
            mid, candidates = bind_subject(n["subject"], compiled)
            if mid is not None:
                n["scope"], n["map_id"] = "map", mid
            else:
                n["scope"], n["candidates"] = "unbound", candidates
        out.append(n)
    return out


def notes_for_map(state: dict, compiled: dict, map_id: str) -> list:
    """Texts of the notes bound to one map — the injection payload for that
    map's content calls (N3)."""
    if not map_id:
        return []
    return [n["text"] for n in bound_notes(state, compiled)
            if n.get("map_id") == map_id]


def notes_matching_name(state: dict, name: str) -> list:
    """Texts of the subject notes whose subject names ``name`` directly —
    the pre-binding variant for content being created right now (a map
    being generated from a designed level has no map record to bind to
    yet)."""
    key = join_key(name)
    if not key:
        return []
    return [n["text"] for n in subject_notes(state)
            if join_key(n["subject"]) == key
            or _subject_matches_name(join_key(n["subject"]), name)
            or _subject_matches_name(key, n["subject"])]


# --- rendering (N3) ----------------------------------------------------------

def seed_notes_block(state: dict) -> str:
    """The world-scoped notes in full plus a one-line index of the subject
    notes, for the seed seam: every generation call sees the world-level
    facts and knows the scoped notes exist; their full texts inject only in
    their own scope. Empty string without notes."""
    parts = []
    world = world_note_texts(state)
    if world:
        parts.append(
            "Established facts agreed with the world's creator — the world "
            "must embody every one:\n" + "\n".join(f"- {t}" for t in world))
    subjects = subject_notes(state)
    if subjects:
        counts: dict = {}
        for n in subjects:
            counts[n["subject"]] = counts.get(n["subject"], 0) + 1
        parts.append(
            "Specific places in this world carry their own agreed design "
            "notes, applied when generating those places — do not spread "
            "them beyond their subject: "
            + "; ".join(f"{s} ({c} note{'s' if c > 1 else ''})"
                        for s, c in counts.items()))
    return "\n\n".join(parts)


def _note_marker(n: dict) -> str:
    nid = n.get("id") or "?"
    if n.get("no_compromise"):
        return (f"[{nid} — a compromise was VETOED by the user: the text "
                "below is binding and must not be amended again]")
    if n.get("status") == "amended":
        return f"[{nid} — amended by compromise, pending the user's review]"
    return f"[{nid}]"


def agent_notes_block(state: dict, compiled: dict) -> str:
    """Every note in full, grouped and annotated with its live binding —
    the build agent's view (N3: the orchestrator sees the whole contract).
    Empty string without notes."""
    notes = bound_notes(state, compiled)
    if not notes:
        return ""
    lines = []
    world = [n for n in notes if n["scope"] == "world"]
    if world:
        lines.append("World-wide:")
        lines.extend(f"- {_note_marker(n)} {n['text']}" for n in world)
    scoped = [n for n in notes if n["scope"] != "world"]
    if scoped:
        lines.append(
            "About specific places (each binds to one map; that map's "
            "content must embody the note):")
        for n in scoped:
            if n["scope"] == "map":
                where = f"→ map '{n['map_id']}'"
            elif n.get("candidates"):
                where = ("→ UNBOUND, matches several maps ambiguously: "
                         + ", ".join(n["candidates"]))
            else:
                where = "→ UNBOUND, nothing matches yet — build it"
            lines.append(
                f"- {_note_marker(n)} \"{n['subject']}\" {where}: {n['text']}")
    return "\n".join(lines)


# --- lint (N2, P7) -----------------------------------------------------------

def lint_notes(state: dict, compiled: dict) -> list:
    """Deterministic note problems: subject notes that bind to nothing (or
    to several maps ambiguously), as blocking lint entries. Checkable
    without an LLM — the offline floor of note verification (N4)."""
    problems = []
    for n in bound_notes(state, compiled):
        if n.get("scope") != "unbound":
            continue
        nid = n.get("id") or "?"
        candidates = n.get("candidates") or []
        if candidates:
            message = (
                f"Agreed note {nid} is about '{n['subject']}', which matches "
                f"several maps ambiguously ({', '.join(candidates)}). Rename "
                "or relabel so the subject matches exactly one place.")
        else:
            message = (
                f"Agreed note {nid} is about '{n['subject']}', but nothing "
                "in the world matches that subject yet. Build it (hierarchy "
                "or map steering, add_node) or name an existing place after "
                "it.")
        problems.append({"kind": "note_unbound", "note_id": nid,
                         "subject": n.get("subject", ""), "message": message})
    return problems
