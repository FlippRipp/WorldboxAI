"""Planar street-network generation (the zero.re "roadnet" algorithm).

Grows a city street graph in two parts. Part 1 scatters intersection
points with minimum clearance (active-queue Poisson-disc growth inside a
boundary polygon) and connects them into streets by greedy street-walking
under hard geometric constraints: planarity (no edge crossings), no
duplicate edges, no triangles, a minimum junction angle and a degree cap.
Part 2 splits long edges into anchor chains, extracts the planar faces
(city blocks) with a rightmost-turn half-edge walk and recursively
densifies large blocks with smaller per-level parameters.

Pure stdlib and fully deterministic for a given seed. Coordinates are in
whatever unit the caller supplies (the world map uses config.map_width
space, 1000x1000).
"""

import math
import random
from dataclasses import dataclass, field

MIN_ANGLE_DEG = 60.0
MAX_DEGREE = 4
_WALK_PASSES = 8  # saturation cap; passes stop early once nothing connects
_SCATTER_TRIES = 12
_SEED_TRIES = 40
_EPS = 1e-9


@dataclass(frozen=True)
class LevelParams:
    """Per-recursion-level knobs (all in map units)."""

    clearance: float        # minimum spacing between intersection points
    extension_min: float    # child spawn distance range from an active point
    extension_max: float
    connect_radius: float   # candidate radius for street connection
    edge_split_len: float   # split edges longer than this before densifying
    min_face_area: float    # only densify faces bigger than this


# Ladder tuned for a 1000x1000 map: avenues, streets, lanes. Thresholds are
# deliberately low so most blocks subdivide — the reference algorithm's look
# is a dense fabric, not a sparse web.
DEFAULT_LEVELS: tuple[LevelParams, ...] = (
    LevelParams(80.0, 100.0, 155.0, 180.0, 130.0, 11000.0),
    LevelParams(34.0, 40.0, 66.0, 80.0, 55.0, 2400.0),
    LevelParams(15.0, 17.0, 29.0, 36.0, math.inf, 0.0),
)

#: How many child generations each level's outward growth may spawn beyond
#: the network's edge (the article's split-number sprawl). Keeps branches
#: tethered to the network instead of flooding the map.
OUTWARD_GENERATIONS = 4

#: Streets rarely dead-end in a real city: tips try to loop back into the
#: fabric, easing from the strict knob down to the bottom of the article's
#: recommended 55-65 degree band — never below it. Unjoinable tips are
#: pruned instead.
_JOIN_ANGLE_DEG = 55.0

#: A street only continues through a candidate that keeps it reasonably
#: straight (bend under ~50 degrees); sharper turns end the street and the
#: node starts a fresh one later. Keeps the fabric grid-like, not maze-like.
_CONTINUE_ALIGN = math.cos(math.radians(50.0))


@dataclass
class RoadNetwork:
    points: list                      # [(x, y), ...]
    edges: set                        # {(a, b), ...} with a < b, planar
    point_levels: list                # per-point recursion level (0 = arterial)
    edge_levels: dict                 # {(a, b): level}
    faces: list = field(default_factory=list)   # interior faces (vertex loops)
    outer_face: list = field(default_factory=list)
    relaxed_edges: set = field(default_factory=set)  # joins that bent the angle/triangle rules
    anchors: set = field(default_factory=set)        # points inserted by edge splitting
    sprawl_points: set = field(default_factory=set)  # outskirts points beyond the boundary

    def degree(self, i: int) -> int:
        return sum(1 for a, b in self.edges if a == i or b == i)


# ---------------------------------------------------------------------------
# Geometry helpers


def _dist(p, q) -> float:
    return math.hypot(p[0] - q[0], p[1] - q[1])


def polygon_area(pts) -> float:
    """Signed shoelace area of a vertex loop."""
    total = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def polygon_centroid(pts):
    """Area centroid of a simple polygon (vertex mean fallback for slivers)."""
    a = polygon_area(pts)
    if abs(a) < _EPS:
        n = len(pts)
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6.0 * a), cy / (6.0 * a))


def point_in_polygon(p, poly) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
            if x < xin:
                inside = not inside
    return inside


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, c) -> bool:
    return (min(a[0], b[0]) - _EPS <= c[0] <= max(a[0], b[0]) + _EPS
            and min(a[1], b[1]) - _EPS <= c[1] <= max(a[1], b[1]) + _EPS)


def segments_intersect(a1, a2, b1, b2) -> bool:
    """True when segments touch or cross anywhere.

    Callers exclude edges that share an endpoint index, so any remaining
    contact (including an endpoint landing on the other segment, as with
    collinear split anchors) counts as a crossing.
    """
    d1 = _orient(b1, b2, a1)
    d2 = _orient(b1, b2, a2)
    d3 = _orient(a1, a2, b1)
    d4 = _orient(a1, a2, b2)
    if ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and \
       ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)):
        return True
    if abs(d1) <= _EPS and _on_segment(b1, b2, a1):
        return True
    if abs(d2) <= _EPS and _on_segment(b1, b2, a2):
        return True
    if abs(d3) <= _EPS and _on_segment(a1, a2, b1):
        return True
    if abs(d4) <= _EPS and _on_segment(a1, a2, b2):
        return True
    return False


def _angle_between_deg(v1, v2) -> float:
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 < _EPS or n2 < _EPS:
        return 0.0
    d = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, d))))


# ---------------------------------------------------------------------------
# Spatial indices


class _PointGrid:
    def __init__(self, cell: float = 40.0):
        self.cell = cell
        self.cells: dict = {}

    def _key(self, p):
        return (int(p[0] // self.cell), int(p[1] // self.cell))

    def add(self, idx, p):
        self.cells.setdefault(self._key(p), []).append(idx)

    def near(self, p, radius):
        """Indices of points in cells within `radius` of p (superset)."""
        r = int(radius // self.cell) + 1
        cx, cy = self._key(p)
        for gx in range(cx - r, cx + r + 1):
            for gy in range(cy - r, cy + r + 1):
                yield from self.cells.get((gx, gy), ())


class _SegIndex:
    def __init__(self, cell: float = 64.0):
        self.cell = cell
        self.cells: dict = {}

    def _keys(self, p1, p2):
        x1 = int(min(p1[0], p2[0]) // self.cell)
        x2 = int(max(p1[0], p2[0]) // self.cell)
        y1 = int(min(p1[1], p2[1]) // self.cell)
        y2 = int(max(p1[1], p2[1]) // self.cell)
        return [(gx, gy) for gx in range(x1, x2 + 1) for gy in range(y1, y2 + 1)]

    def add(self, edge, p1, p2):
        for key in self._keys(p1, p2):
            self.cells.setdefault(key, set()).add(edge)

    def remove(self, edge, p1, p2):
        for key in self._keys(p1, p2):
            self.cells.get(key, set()).discard(edge)

    def near(self, p1, p2):
        found = set()
        for key in self._keys(p1, p2):
            found |= self.cells.get(key, set())
        return found


# ---------------------------------------------------------------------------
# Mutable build state


class _NetState:
    def __init__(self):
        self.points: list = []
        self.point_levels: list = []
        self.adj: list = []
        self.edges: set = set()
        self.edge_levels: dict = {}
        self.grid = _PointGrid()
        self.segs = _SegIndex()
        self.removed: set = set()
        self.relaxed_edges: set = set()
        self.anchors: set = set()
        self.sprawl_pts: set = set()

    def add_point(self, p, level) -> int:
        idx = len(self.points)
        self.points.append((float(p[0]), float(p[1])))
        self.point_levels.append(level)
        self.adj.append(set())
        self.grid.add(idx, p)
        return idx

    def degree(self, i) -> int:
        return len(self.adj[i])

    def add_edge(self, a, b, level):
        e = (a, b) if a < b else (b, a)
        self.edges.add(e)
        self.edge_levels[e] = level
        self.adj[a].add(b)
        self.adj[b].add(a)
        self.segs.add(e, self.points[e[0]], self.points[e[1]])

    def remove_edge(self, e):
        self.edges.discard(e)
        self.edge_levels.pop(e, None)
        a, b = e
        self.adj[a].discard(b)
        self.adj[b].discard(a)
        self.segs.remove(e, self.points[a], self.points[b])

    def remove_point(self, i):
        for n in list(self.adj[i]):
            self.remove_edge((i, n) if i < n else (n, i))
        self.removed.add(i)

    def clear_at(self, p, clearance) -> bool:
        for j in set(self.grid.near(p, clearance)):
            if j in self.removed:
                continue
            if _dist(p, self.points[j]) < clearance:
                return False
        return True

    def crosses(self, a, b) -> bool:
        pa, pb = self.points[a], self.points[b]
        for e in self.segs.near(pa, pb):
            if e not in self.edges:
                continue
            c, d = e
            if c in (a, b) or d in (a, b):
                continue
            if segments_intersect(pa, pb, self.points[c], self.points[d]):
                return True
        return False

    def angle_ok(self, a, b, min_deg) -> bool:
        pa, pb = self.points[a], self.points[b]
        for u, other in ((a, b), (b, a)):
            pu = self.points[u]
            po = self.points[other]
            vu = (po[0] - pu[0], po[1] - pu[1])
            for n in self.adj[u]:
                if n == other:
                    continue
                pn = self.points[n]
                if _angle_between_deg(vu, (pn[0] - pu[0], pn[1] - pu[1])) < min_deg:
                    return False
        return True

    def edge_valid(self, a, b, min_deg=MIN_ANGLE_DEG, *,
                   ignore_triangle=False, ignore_angle=False) -> bool:
        if a == b:
            return False
        e = (a, b) if a < b else (b, a)
        if e in self.edges:
            return False
        if self.degree(a) >= MAX_DEGREE or self.degree(b) >= MAX_DEGREE:
            return False
        if not ignore_triangle and self.adj[a] & self.adj[b]:
            return False
        if not ignore_angle and not self.angle_ok(a, b, min_deg):
            return False
        if self.crosses(a, b):
            return False
        return True


# ---------------------------------------------------------------------------
# Part 1a: scatter


def _sample_in_polygon(rng, poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    for _ in range(_SEED_TRIES):
        p = (rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys)))
        if point_in_polygon(p, poly):
            return p
    return None


def scatter_points(state: _NetState, rng: random.Random, level: int,
                   params: LevelParams, polygon, seeds=None,
                   max_generations=None) -> list:
    """Article's node scatter: FIFO open list, spawn-all-valid-candidates.

    Each open node is processed once and emits 10-16 candidates evenly
    around a circle at random distances in the extension range; EVERY
    candidate that is inside ``polygon`` and clear of all existing points
    is added and queued. Growth starts from one fresh sample inside the
    polygon, or radiates from ``seeds`` (existing point indices — e.g. a
    face's boundary vertices, which count for clearance but are not
    re-emitted). ``max_generations`` is the article's split number: how
    many child generations may spawn. Returns new indices.
    """
    new_pts = []
    if seeds is None:
        seed_pt = None
        for _ in range(_SEED_TRIES):
            p = _sample_in_polygon(rng, polygon)
            if p is not None and state.clear_at(p, params.clearance):
                seed_pt = p
                break
        if seed_pt is None:
            return new_pts
        idx = state.add_point(seed_pt, level)
        new_pts.append(idx)
        open_list = [(idx, 0)]
    else:
        open_list = [(s, 0) for s in seeds if s not in state.removed]
    head = 0
    while head < len(open_list):
        base_idx, gen = open_list[head]
        head += 1
        if max_generations is not None and gen >= max_generations:
            continue
        base = state.points[base_idx]
        k = rng.randint(10, 16)
        ang0 = rng.uniform(0.0, 2.0 * math.pi)
        for i in range(k):
            ang = ang0 + 2.0 * math.pi * i / k
            d = rng.uniform(params.extension_min, params.extension_max)
            p = (base[0] + d * math.cos(ang), base[1] + d * math.sin(ang))
            if not point_in_polygon(p, polygon):
                continue
            if not state.clear_at(p, params.clearance):
                continue
            idx = state.add_point(p, level)
            new_pts.append(idx)
            open_list.append((idx, gen + 1))
    return new_pts


# ---------------------------------------------------------------------------
# Part 1b: connect


def connect_points(state: _NetState, rng: random.Random, candidates,
                   params: LevelParams, level: int,
                   min_deg=MIN_ANGLE_DEG) -> int:
    """Street creation per the article: from each unsaturated node, pick a
    direction and grow a street by repeatedly linking the best-scoring valid
    candidate (alignment with the previous edge + proximity), until no valid
    node remains. Nodes are revisited (new random direction each pass) until
    a full pass adds no edges — full saturation. Returns edges added."""
    cset = {c for c in candidates if c not in state.removed}
    added = 0
    for _ in range(_WALK_PASSES):
        pass_added = 0
        order = sorted(cset, key=lambda i: (state.degree(i), i))
        for start in order:
            if state.degree(start) >= MAX_DEGREE:
                continue
            ang = rng.uniform(0.0, 2.0 * math.pi)
            cur, prev_dir = start, (math.cos(ang), math.sin(ang))
            walking = False
            while state.degree(cur) < MAX_DEGREE:
                pc = state.points[cur]
                best, best_score = None, -math.inf
                for j in sorted(set(state.grid.near(pc, params.connect_radius))):
                    if j == cur or j not in cset:
                        continue
                    pj = state.points[j]
                    d = _dist(pc, pj)
                    if d < _EPS or d > params.connect_radius:
                        continue
                    vd = ((pj[0] - pc[0]) / d, (pj[1] - pc[1]) / d)
                    align = vd[0] * prev_dir[0] + vd[1] * prev_dir[1]
                    if walking and align < _CONTINUE_ALIGN:
                        continue  # would bend the street too sharply
                    if not state.edge_valid(cur, j, min_deg):
                        continue
                    prox = 1.0 - d / params.connect_radius
                    score = 0.75 * align + 0.25 * prox
                    if score > best_score:
                        best_score, best = score, j
                if best is None:
                    break
                pb = state.points[best]
                d = _dist(pc, pb)
                state.add_edge(cur, best, level)
                added += 1
                pass_added += 1
                prev_dir = ((pb[0] - pc[0]) / d, (pb[1] - pc[1]) / d)
                cur = best
                walking = True
        if pass_added == 0:
            break
    return added


def _components(state: _NetState, members) -> list:
    """Connected components of the subgraph induced by `members`."""
    members = {m for m in members if m not in state.removed}
    seen = set()
    comps = []
    for m in sorted(members):
        if m in seen:
            continue
        comp = {m}
        queue = [m]
        seen.add(m)
        while queue:
            u = queue.pop()
            for n in state.adj[u]:
                if n in members and n not in seen:
                    seen.add(n)
                    comp.add(n)
                    queue.append(n)
        comps.append(comp)
    return comps


def _join_components(state: _NetState, members, params: LevelParams,
                     level: int, anchors=None):
    """Merge disconnected components with the shortest valid edges.

    Only the triangle rule may be relaxed — planarity, duplicates, the
    degree cap and the minimum junction angle always hold. Components that
    still cannot be reached are removed (anchored components survive).
    """
    reach = 2.5 * params.connect_radius
    for relax in ({}, {"ignore_triangle": True}):
        while True:
            comps = _components(state, members)
            if len(comps) <= 1:
                return
            comps.sort(key=len, reverse=True)
            main = comps[0]
            best = None
            for comp in comps[1:]:
                for a in sorted(comp):
                    pa = state.points[a]
                    for b in sorted(main):
                        d = _dist(pa, state.points[b])
                        if d > reach:
                            continue
                        if best is not None and d >= best[0]:
                            continue
                        if state.edge_valid(a, b, **relax):
                            best = (d, a, b)
            if best is None:
                break
            state.add_edge(best[1], best[2], level)
            if relax:
                a, b = best[1], best[2]
                state.relaxed_edges.add((a, b) if a < b else (b, a))
    comps = _components(state, members)
    if len(comps) <= 1:
        return
    anchors = set(anchors or ())
    comps.sort(key=len, reverse=True)
    keep_done = False
    for comp in comps:
        anchored = bool(comp & anchors)
        if anchored or (not keep_done and not anchors):
            keep_done = keep_done or not anchored
            continue
        for i in comp:
            state.remove_point(i)


# ---------------------------------------------------------------------------
# Part 2a: split


def split_long_edges(state: _NetState, max_len: float):
    if not math.isfinite(max_len) or max_len <= 0:
        return
    for e in sorted(state.edges):
        a, b = e
        pa, pb = state.points[a], state.points[b]
        length = _dist(pa, pb)
        if length <= max_len:
            continue
        pieces = int(math.ceil(length / max_len))
        level = state.edge_levels.get(e, 0)
        relaxed = e in state.relaxed_edges
        state.remove_edge(e)
        state.relaxed_edges.discard(e)
        prev = a
        for k in range(1, pieces):
            t = k / pieces
            mid = (pa[0] + (pb[0] - pa[0]) * t, pa[1] + (pb[1] - pa[1]) * t)
            idx = state.add_point(mid, level)
            state.anchors.add(idx)
            state.add_edge(prev, idx, level)
            if relaxed:
                state.relaxed_edges.add((prev, idx) if prev < idx else (idx, prev))
            prev = idx
        state.add_edge(prev, b, level)
        if relaxed:
            state.relaxed_edges.add((prev, b) if prev < b else (b, prev))


# ---------------------------------------------------------------------------
# Part 2b: faces


def extract_faces(points, edges):
    """Interior faces of a planar graph via rightmost-turn half-edge walk.

    Dead-end spurs (iteratively pruned degree-1 vertices) are excluded so
    faces are proper blocks. Returns (interior_faces, outer_face) as vertex
    index loops; the outer face is the loop with the largest absolute area.
    """
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    # Prune to the 2-core so bridges/spurs don't produce slit faces.
    stack = [v for v, ns in adj.items() if len(ns) <= 1]
    while stack:
        v = stack.pop()
        for n in adj.get(v, ()):
            adj[n].discard(v)
            if len(adj[n]) == 1:
                stack.append(n)
        adj.pop(v, None)
    order = {}
    for v, ns in adj.items():
        pv = points[v]
        order[v] = sorted(ns, key=lambda n: math.atan2(
            points[n][1] - pv[1], points[n][0] - pv[0]))
    visited = set()
    loops = []
    for v in sorted(adj):
        for w in order[v]:
            if (v, w) in visited:
                continue
            loop = []
            cur = (v, w)
            while cur not in visited:
                visited.add(cur)
                loop.append(cur[0])
                u, x = cur
                ring = order[x]
                # Rightmost turn: next clockwise neighbor after the reverse edge.
                nxt = ring[(ring.index(u) - 1) % len(ring)]
                cur = (x, nxt)
            loops.append(loop)
    if not loops:
        return [], []
    areas = [abs(polygon_area([points[i] for i in loop])) for loop in loops]
    outer_at = max(range(len(loops)), key=lambda i: areas[i])
    interior = [loop for i, loop in enumerate(loops)
                if i != outer_at and len(loop) >= 3]
    return interior, loops[outer_at]


# ---------------------------------------------------------------------------
# Part 2c: densify + entry point


def _densify_faces(state: _NetState, rng: random.Random, params: LevelParams,
                   next_params: LevelParams, next_level: int):
    live_points = state.points
    faces, _outer = extract_faces(
        live_points, {e for e in state.edges})
    faces.sort(key=lambda f: min(f))
    for face in faces:
        poly = [live_points[i] for i in face]
        if abs(polygon_area(poly)) <= params.min_face_area:
            continue
        boundary = set(face)
        # Per the article, the boundary vertices themselves seed the fill.
        new_pts = scatter_points(state, rng, next_level, next_params, poly,
                                 seeds=sorted(boundary))
        if not new_pts:
            continue
        connect_points(state, rng, boundary | set(new_pts), next_params,
                       next_level)
        _join_components(state, boundary | set(new_pts), next_params,
                         next_level, anchors=boundary)


def _grow_outward(state: _NetState, rng: random.Random, params: LevelParams,
                  level: int, width: float, height: float, generations: int):
    """Branch the next-level streets outward from the network's outer edge.

    Seeds are the current outer-face vertices; growth is bounded to
    ``generations`` child generations so each level's reach beyond the edge
    is a few extension lengths — dense core fading into outskirts, level by
    level, per the article's outer-face sprawl."""
    margin = params.clearance / 3.0
    rect = [(margin, margin), (width - margin, margin),
            (width - margin, height - margin), (margin, height - margin)]
    _faces, outer = extract_faces(
        state.points, {e for e in state.edges if e[0] not in state.removed
                       and e[1] not in state.removed})
    anchors = {v for v in outer if v not in state.removed}
    if not anchors:
        return
    new_pts = scatter_points(state, rng, level, params, rect,
                             seeds=sorted(anchors),
                             max_generations=generations)
    if not new_pts:
        return
    state.sprawl_pts.update(new_pts)
    members = set(new_pts) | anchors
    connect_points(state, rng, members, params, level)
    _join_components(state, members, params, level, anchors=anchors)


def _close_dead_ends(state: _NetState, levels):
    """Loop the network: join every degree-1 tip back into the fabric.

    Real street grids are meshes, not trees — a dead end is the exception.
    Each tip tries the nearest joinable street vertex, easing the junction
    angle down to the article's 55-degree floor and finally waiving the
    triangle rule (those joins are tagged relaxed). The minimum angle is
    never broken. Tips that still cannot loop back are pruned, cascading up
    their spur chain.
    """
    def try_join(v) -> bool:
        level = state.point_levels[v]
        params = levels[min(level, len(levels) - 1)]
        radius = params.connect_radius * 1.5
        pv = state.points[v]
        cands = sorted(
            (j for j in set(state.grid.near(pv, radius))
             if j != v and j not in state.removed
             and 0 < _dist(pv, state.points[j]) <= radius),
            key=lambda j: (_dist(pv, state.points[j]), j))
        for min_deg, relax in ((MIN_ANGLE_DEG, {}),
                               (_JOIN_ANGLE_DEG, {}),
                               (_JOIN_ANGLE_DEG, {"ignore_triangle": True})):
            for j in cands:
                if state.edge_valid(v, j, min_deg, **relax):
                    state.add_edge(v, j, level)
                    if relax:
                        e = (v, j) if v < j else (j, v)
                        state.relaxed_edges.add(e)
                    return True
        return False

    for _ in range(50):
        tips = [v for v in range(len(state.points))
                if v not in state.removed and state.degree(v) == 1]
        progress = False
        for v in tips:
            if v in state.removed or state.degree(v) != 1:
                continue
            if try_join(v):
                progress = True
            else:
                state.remove_point(v)
                progress = True
        if not progress or not tips:
            break


def generate_roadnet(seed, width: float, height: float,
                     levels=DEFAULT_LEVELS, boundary=None,
                     outward_generations: int = 0) -> RoadNetwork:
    """Generate a planar street network; see module docstring.

    With ``outward_generations`` > 0 (requires a ``boundary``), each
    recursion level also branches its streets outward from the network's
    current outer edge — big roads first, then smaller roads growing both
    inside the blocks and beyond the edge, recursively.
    """
    rng = random.Random(seed)
    if boundary is None:
        boundary = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
        outward_generations = 0
    state = _NetState()
    scatter_points(state, rng, 0, levels[0], boundary)
    all_pts = set(range(len(state.points)))
    connect_points(state, rng, all_pts, levels[0], 0)
    _join_components(state, all_pts, levels[0], 0)
    for li in range(len(levels) - 1):
        split_long_edges(state, levels[li].edge_split_len)
        _densify_faces(state, rng, levels[li], levels[li + 1], li + 1)
        if outward_generations > 0:
            _grow_outward(state, rng, levels[li + 1], li + 1, width, height,
                          outward_generations)
    _close_dead_ends(state, levels)

    # Compact: drop removed and never-connected points, remap indices.
    alive = [i for i in range(len(state.points))
             if i not in state.removed and state.degree(i) > 0]
    remap = {old: new for new, old in enumerate(alive)}
    points = [state.points[i] for i in alive]
    point_levels = [state.point_levels[i] for i in alive]
    edges = set()
    edge_levels = {}
    for (a, b), lvl in state.edge_levels.items():
        e = (remap[a], remap[b]) if remap[a] < remap[b] else (remap[b], remap[a])
        edges.add(e)
        edge_levels[e] = lvl
    net = RoadNetwork(points=points, edges=edges, point_levels=point_levels,
                      edge_levels=edge_levels)
    net.anchors = {remap[i] for i in state.anchors if i in remap}
    net.sprawl_points = {remap[i] for i in state.sprawl_pts if i in remap}
    net.relaxed_edges = {
        (remap[a], remap[b]) if remap[a] < remap[b] else (remap[b], remap[a])
        for a, b in state.relaxed_edges
        if a in remap and b in remap and
        ((remap[a], remap[b]) if remap[a] < remap[b] else (remap[b], remap[a])) in edges}
    net.faces, net.outer_face = extract_faces(points, edges)
    return net
