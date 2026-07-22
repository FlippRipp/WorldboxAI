"""Codex reads, binding and lint — the lorebook's pure query surface.

The ``codex`` step PRODUCES the data (AI-declared domains + reference
entries); everything that reads it — the enrichment context, the evaluator
excerpts, the lints, the play-time embed — asks this module, so the codex
has one read seam (the ``notes.py`` precedent, for the lorebook).

Pure functions over plain dicts (no I/O, no services). All take the
compiled world: the step's ``contribute_to_compiled`` copies its data to
``compiled["codex"]``. Subject binding is ``notes.bind_subject`` verbatim —
one binding semantics for everything subject-shaped (N2).
"""

from wbworldgen.mapmodel import join_key
from wbworldgen.worldgen.notes import bind_subject


def codex_data(compiled: dict) -> dict:
    """The codex payload from either shape in hand: the compiled world
    (``contribute_to_compiled`` copies it to ``compiled["codex"]``) or the
    raw world state (the step's own data — for seams that run pre-compile,
    the notes take-either-dict precedent)."""
    data = (compiled or {}).get("codex")
    if isinstance(data, dict):
        return data
    data = (((compiled or {}).get("steps") or {}).get("codex") or {}).get("data")
    return data if isinstance(data, dict) else {}


def domain_names(compiled: dict) -> list:
    """Declared domain names, cleaned, order kept."""
    out = []
    for d in codex_data(compiled).get("domains") or []:
        name = str(d.get("name") or "").strip() if isinstance(d, dict) else str(d).strip()
        if name:
            out.append(name)
    return out


def entries(compiled: dict) -> list:
    """Cleaned entries: non-empty name plus at least a summary or details;
    ``domain``/``subject`` trimmed strings."""
    out = []
    for e in codex_data(compiled).get("entries") or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        summary = str(e.get("summary") or "").strip()
        details = str(e.get("details") or "").strip()
        if not name or not (summary or details):
            continue
        out.append({
            "domain": str(e.get("domain") or "").strip(),
            "name": name,
            "summary": summary,
            "details": details,
            "subject": str(e.get("subject") or "").strip(),
        })
    return out


def bound_entries(compiled: dict) -> list:
    """Every entry with its live binding: adds ``scope`` (``"world"`` |
    ``"map"`` | ``"unbound"``), ``map_id`` (map scope) and ``candidates``
    (ambiguous matches, unbound only). Binding is recomputed against the
    compiled world whenever needed, never stored (maps appear and rename
    mid-build)."""
    out = []
    for e in entries(compiled):
        e = dict(e)
        if not e["subject"]:
            e["scope"] = "world"
        else:
            mid, candidates = bind_subject(e["subject"], compiled)
            if mid is not None:
                e["scope"], e["map_id"] = "map", mid
            else:
                e["scope"], e["candidates"] = "unbound", candidates
        out.append(e)
    return out


def node_context_block(compiled: dict, map_id: str) -> list:
    """The codex as one node-level content call sees it (the N3 visibility
    rule, applied to reference): world-wide entries by summary — the
    structural unit written for exactly this — and the entries bound to the
    node's own map in full."""
    block = []
    for e in bound_entries(compiled):
        if e["scope"] == "world":
            block.append({"domain": e["domain"], "name": e["name"],
                          "summary": e["summary"] or e["details"]})
        elif e["scope"] == "map" and map_id and e.get("map_id") == map_id:
            local = {"domain": e["domain"], "name": e["name"],
                     "summary": e["summary"]}
            if e["details"]:
                local["details"] = e["details"]
            block.append(local)
    return block


def entries_matching_name(compiled: dict, name: str) -> list:
    """Full texts of the subject entries whose subject names ``name``
    directly — the pre-binding variant for content being created right now
    (a child map being authored has no map record to bind to yet; the
    ``notes_matching_name`` precedent). One rendered string per entry."""
    from wbworldgen.worldgen.notes import _subject_matches_name
    key = join_key(name)
    if not key:
        return []
    out = []
    for e in entries(compiled):
        if not e["subject"]:
            continue
        skey = join_key(e["subject"])
        if (skey == key or _subject_matches_name(skey, name)
                or _subject_matches_name(key, e["subject"])):
            body = " ".join(t for t in (e["summary"], e["details"]) if t)
            out.append(f"{e['name']} ({e['domain']}): {body}" if e["domain"]
                       else f"{e['name']}: {body}")
    return out


def render_excerpt(compiled: dict) -> str:
    """The codex rendered for the evaluator's critique excerpt: every entry
    in full, grouped by domain, subject bindings annotated. Empty string
    without a codex."""
    bound = bound_entries(compiled)
    if not bound:
        return ""
    lines = ["Codex (the world's reference lore — content must not contradict it):"]
    for domain in domain_names(compiled) or sorted({e["domain"] for e in bound}):
        in_domain = [e for e in bound if join_key(e["domain"]) == join_key(domain)]
        if not in_domain:
            lines.append(f"  [{domain}]: NO ENTRIES (declared but empty)")
            continue
        lines.append(f"  [{domain}]")
        for e in in_domain:
            where = ""
            if e["scope"] == "map":
                where = f" (about map '{e['map_id']}' only)"
            elif e["scope"] == "unbound":
                where = f" (subject '{e['subject']}' matches nothing)"
            body = " ".join(t for t in (e["summary"], e["details"]) if t)
            lines.append(f"  - {e['name']}{where}: {body}")
    stray = [e for e in bound
             if e["domain"] and join_key(e["domain"]) not in
             {join_key(d) for d in domain_names(compiled)}]
    for e in stray:
        lines.append(f"  - {e['name']} [undeclared domain '{e['domain']}']: "
                     + " ".join(t for t in (e["summary"], e["details"]) if t))
    return "\n".join(lines)


# --- lint (P7) ---------------------------------------------------------------

def lint_codex(compiled: dict) -> list:
    """Deterministic codex problems, all blocking: a declared domain with no
    entries (the declaration is a checkable obligation — the point of
    declaring), an entry under a domain nobody declared, and a subject that
    binds to nothing or to several maps ambiguously (the ``note_unbound``
    pattern). Worlds without a codex no-op."""
    problems = []
    declared = domain_names(compiled)
    declared_keys = {join_key(d) for d in declared}
    bound = bound_entries(compiled)

    for domain in declared:
        if not any(join_key(e["domain"]) == join_key(domain) for e in bound):
            problems.append({
                "kind": "codex_domain_empty", "domain": domain,
                "message": (f"Codex domain '{domain}' is declared but has no "
                            "entries. Write at least one entry for it "
                            "(run_step codex with a steering note, or "
                            "patch_step), or drop the domain if the world "
                            "truly does not need it.")})

    for e in bound:
        if e["domain"] and join_key(e["domain"]) not in declared_keys:
            problems.append({
                "kind": "codex_unknown_domain", "entry": e["name"],
                "domain": e["domain"],
                "message": (f"Codex entry '{e['name']}' is filed under "
                            f"'{e['domain']}', which is not a declared domain. "
                            "Declare the domain or refile the entry.")})
        if e["scope"] != "unbound":
            continue
        candidates = e.get("candidates") or []
        if candidates:
            message = (f"Codex entry '{e['name']}' is about "
                       f"'{e['subject']}', which matches several maps "
                       f"ambiguously ({', '.join(candidates)}). Rename or "
                       "relabel so the subject matches exactly one place.")
        else:
            message = (f"Codex entry '{e['name']}' is about "
                       f"'{e['subject']}', but nothing in the world matches "
                       "that subject. Build the place, name an existing one "
                       "after it, or clear the entry's subject.")
        problems.append({"kind": "codex_unbound", "entry": e["name"],
                         "subject": e["subject"], "message": message})
    return problems
