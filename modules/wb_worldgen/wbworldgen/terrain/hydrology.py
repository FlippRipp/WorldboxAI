"""Hydrology core: depression filling, D-infinity flow, accumulation, Strahler.

This is the principled pipeline from the research synthesis:

    Priority-Flood + epsilon   (guarantee drainage to the sea, no flats)
    -> D-infinity flow (Tarboton 1997)   (continuous direction, no 45deg bias)
    -> flow accumulation                  (discharge per cell)
    -> Horton-Strahler ordering           (branch complexity -> river width)

D-infinity routes each cell's flow across the steepest of eight triangular
facets, proportioning discharge between the two bounding neighbours. This
removes the rigid grid bias of D8 while staying crisp (unlike full MFD). For the
discrete river *tree* (tracing + Strahler) we take the dominant receiver.

Refs: Barnes et al. "Priority-Flood" (arXiv:1511.04463); Tarboton (1997)
D-infinity; Horton-Strahler stream order.
"""

import numpy as np

try:
    from numba import njit
    _HAS_NUMBA = True
except ImportError:  # pragma: no cover
    _HAS_NUMBA = False

    def njit(*a, **k):
        def wrap(fn):
            return fn
        return wrap(a[0]) if a and callable(a[0]) else wrap


# Tarboton's 8 facets: (e1 cardinal dy,dx) and (e2 diagonal dy,dx).
_FACETS = np.array([
    [0, 1, -1, 1],
    [-1, 0, -1, 1],
    [-1, 0, -1, -1],
    [0, -1, -1, -1],
    [0, -1, 1, -1],
    [1, 0, 1, -1],
    [1, 0, 1, 1],
    [0, 1, 1, 1],
], dtype=np.int64)

_PI4 = np.pi / 4.0


# --------------------------------------------------------------------------
# Priority-Flood + epsilon depression filling (numba array-heap)
# --------------------------------------------------------------------------

@njit(cache=True)
def _hpush(pri, idx, size, p, i):
    pri[size] = p
    idx[size] = i
    c = size
    size += 1
    while c > 0:
        par = (c - 1) // 2
        if pri[par] <= pri[c]:
            break
        pri[par], pri[c] = pri[c], pri[par]
        idx[par], idx[c] = idx[c], idx[par]
        c = par
    return size


@njit(cache=True)
def _hpop(pri, idx, size):
    rp = pri[0]
    ri = idx[0]
    size -= 1
    pri[0] = pri[size]
    idx[0] = idx[size]
    c = 0
    while True:
        l = 2 * c + 1
        r = 2 * c + 2
        sm = c
        if l < size and pri[l] < pri[sm]:
            sm = l
        if r < size and pri[r] < pri[sm]:
            sm = r
        if sm == c:
            break
        pri[c], pri[sm] = pri[sm], pri[c]
        idx[c], idx[sm] = idx[sm], idx[c]
        c = sm
    return rp, ri, size


@njit(cache=True)
def _pflood(filled, sea_level, eps):
    res = filled.shape[0]
    n = res * res
    fl = filled.ravel()
    visited = np.zeros(n, np.bool_)
    pri = np.empty(n + 1, np.float64)
    idx = np.empty(n + 1, np.int64)
    size = 0
    for y in range(res):
        for x in range(res):
            i = y * res + x
            if y == 0 or x == 0 or y == res - 1 or x == res - 1 or fl[i] <= sea_level:
                visited[i] = True
                size = _hpush(pri, idx, size, fl[i], i)
    while size > 0:
        e, i, size = _hpop(pri, idx, size)
        y = i // res
        x = i % res
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                if dy == 0 and dx == 0:
                    continue
                ny = y + dy
                nx = x + dx
                if 0 <= ny < res and 0 <= nx < res:
                    j = ny * res + nx
                    if not visited[j]:
                        visited[j] = True
                        if fl[j] <= e:
                            fl[j] = e + eps
                        size = _hpush(pri, idx, size, fl[j], j)
    return filled


def fill_depressions(height, sea_level, eps=1e-6):
    filled = np.ascontiguousarray(height, dtype=np.float64).copy()
    if _HAS_NUMBA:
        return _pflood(filled, sea_level, eps)
    # Pure-python fallback.
    import heapq
    res = filled.shape[0]
    visited = np.zeros((res, res), np.bool_)
    heap = []
    for y in range(res):
        for x in range(res):
            if y in (0, res - 1) or x in (0, res - 1) or filled[y, x] <= sea_level:
                visited[y, x] = True
                heap.append((filled[y, x], y, x))
    heapq.heapify(heap)
    while heap:
        e, y, x = heapq.heappop(heap)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == dx == 0:
                    continue
                ny, nx = y + dy, x + dx
                if 0 <= ny < res and 0 <= nx < res and not visited[ny, nx]:
                    visited[ny, nx] = True
                    if filled[ny, nx] <= e:
                        filled[ny, nx] = e + eps
                    heapq.heappush(heap, (filled[ny, nx], ny, nx))
    return filled


# --------------------------------------------------------------------------
# D-infinity flow direction + accumulation
# --------------------------------------------------------------------------

@njit(cache=True)
def _dinf(filled, sea_level, facets):
    """Return (recv1, recv2, prop1) flat arrays. recv2=-1 when flow is to a
    single cell; recv=-1 for ocean/pit sinks. prop1 is the fraction to recv1."""
    res = filled.shape[0]
    n = res * res
    fl = filled.ravel()
    recv1 = np.full(n, -1, np.int64)
    recv2 = np.full(n, -1, np.int64)
    prop1 = np.ones(n, np.float64)
    SQRT2 = 1.4142135623730951
    for y in range(res):
        for x in range(res):
            i = y * res + x
            if fl[i] <= sea_level:
                continue
            best_s = 0.0
            best_r = -1.0
            best_e1 = -1
            best_e2 = -1
            z0 = fl[i]
            for fct in range(8):
                y1 = y + facets[fct, 0]; x1 = x + facets[fct, 1]
                y2 = y + facets[fct, 2]; x2 = x + facets[fct, 3]
                if y1 < 0 or y1 >= res or x1 < 0 or x1 >= res:
                    continue
                if y2 < 0 or y2 >= res or x2 < 0 or x2 >= res:
                    continue
                e1 = y1 * res + x1
                e2 = y2 * res + x2
                s1 = z0 - fl[e1]            # cardinal, d=1
                s2 = (fl[e1] - fl[e2])      # orthogonal, d=1
                r = np.arctan2(s2, s1)
                s = np.sqrt(s1 * s1 + s2 * s2)
                if r < 0.0:
                    r = 0.0
                    s = s1
                elif r > _PI4_NB:
                    r = _PI4_NB
                    s = (z0 - fl[e2]) / SQRT2
                if s > best_s:
                    best_s = s
                    best_r = r
                    best_e1 = e1
                    best_e2 = e2
            if best_e1 >= 0 and best_s > 0.0:
                p2 = best_r / _PI4_NB        # fraction toward the diagonal e2
                recv1[i] = best_e1
                recv2[i] = best_e2
                prop1[i] = 1.0 - p2
    return recv1, recv2, prop1


# numba needs a module-level constant inside njit scope
_PI4_NB = _PI4


@njit(cache=True)
def _accumulate(recv1, recv2, prop1, order, weight):
    n = recv1.shape[0]
    acc = weight.copy()
    for k in range(n):
        i = order[k]
        a = acc[i]
        r1 = recv1[i]
        if r1 < 0:
            continue
        p1 = prop1[i]
        acc[r1] += a * p1
        r2 = recv2[i]
        if r2 >= 0 and p1 < 1.0:
            acc[r2] += a * (1.0 - p1)
    return acc


def flow_and_accumulation(filled, sea_level, weight=None):
    """D-infinity flow + accumulation. Returns dict with flat arrays and the
    2D accumulation/primary-receiver fields."""
    res = filled.shape[0]
    fc = np.ascontiguousarray(filled, dtype=np.float64)
    recv1, recv2, prop1 = _dinf(fc, float(sea_level), _FACETS)
    order = np.argsort(fc.ravel())[::-1].astype(np.int64)  # high -> low
    w = np.ones(res * res) if weight is None else weight.ravel().astype(np.float64)
    acc = _accumulate(recv1, recv2, prop1, order, w)

    # Dominant (primary) receiver for the discrete river tree.
    primary = np.where(prop1 >= 0.5, recv1, recv2)
    primary = np.where(recv1 < 0, -1, primary)
    return {
        "acc": acc.reshape(res, res),
        "recv1": recv1, "recv2": recv2, "prop1": prop1,
        "primary": primary.reshape(res, res),
    }


# --------------------------------------------------------------------------
# Horton-Strahler stream ordering
# --------------------------------------------------------------------------

def strahler_order(primary, river_mask, acc):
    """Compute Horton-Strahler order for every river cell.

    Leaf (headwater) = order 1. Joining two streams of equal max order
    increments; otherwise the max is kept. Processed leaves-first using
    ascending accumulation (children always have less discharge than parents).
    """
    res = primary.shape[0]
    order = np.zeros((res, res), np.int32)
    ys, xs = np.nonzero(river_mask)
    if len(xs) == 0:
        return order

    # children[parent_flat] -> list of child flat indices (river cells only).
    children = {}
    for y, x in zip(ys, xs):
        r = int(primary[y, x])
        if r < 0:
            continue
        ry, rx = r // res, r % res
        if river_mask[ry, rx]:
            children.setdefault(r, []).append(y * res + x)

    accv = acc[ys, xs]
    seq = np.argsort(accv)  # ascending discharge -> leaves first
    for k in seq:
        y, x = int(ys[k]), int(xs[k])
        kids = children.get(y * res + x)
        if not kids:
            order[y, x] = 1
            continue
        maxo = 0
        cnt = 0
        for c in kids:
            o = order[c // res, c % res]
            if o > maxo:
                maxo = o
                cnt = 1
            elif o == maxo:
                cnt += 1
        order[y, x] = maxo + 1 if cnt >= 2 else max(1, maxo)
    return order
