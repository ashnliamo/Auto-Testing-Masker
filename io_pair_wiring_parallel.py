import csv
import re
import sys
import math
import pathlib

import gdstk

HERE = pathlib.Path(__file__).parent
INPUT_CSV = HERE / "inputs" / "pinout_grouped.csv"
OUTPUT_DIR = HERE / "outputs"

PAD_SIZE = 80.0
WIRE_WIDTH = 3.0           # aluminium trace width (narrow -> compact combs, R ~ 1/W^2)
WIRE_SPACE = 3.0           # gap between adjacent comb teeth
COIL_GAP = 38.0            # gap between the pad edge and the first comb tooth
VIA_SIZE = 8.0             # via stitching a 2nd-layer wire up to its top-layer pad

# The shared node is a big rectangular PLANE filling the whole interior of the ring
PLANE_RING_MARGIN = 70.0   # keep the plane this far inside the ring (clears pads)
FINGER_OVERLAP = 60.0      # how far each output finger / return reaches into the plane

# Aluminium-on-Ti resistor: R = SHEET_RES * L / W  (sheet res = rho / thickness).
METAL_THICKNESS_UM = 0.1     # 1000 angstrom
AL_RESISTIVITY = 3.243e-8    # ohm*m (Al on 10 A Ti)
SHEET_RES = AL_RESISTIVITY / (METAL_THICKNESS_UM * 1e-6)   # ohm/square (~0.324)
COIL_BASE_R = 500.0          # ohms for the smallest coil in a group
MAX_BINARY_INPUTS = 6

CALIB_COUNT = 8              # number of calibration resistors (first N ladder steps)
CALIB_PAD = 1500.0           # big square probe pad (um) -- >= 1.5 mm for a multimeter
CALIB_LABEL = 150.0          # label height (um) on the calibration coupon
CALIB_GAP = 200.0            # spacing between calibration columns / pads (and margin)

ADD_LABELS = True
LABEL_SIZE = 12.0
ADD_DIE_OUTLINE = True
DIE_MARGIN_BUFFER = 100.0    # clearance beyond the farthest-protruding resistor (um)
DIE_W = DIE_H = 0.0          # set in main()

METAL_BASE, METAL_DT = 1, 0
VIA_BASE, VIA_DT = 20, 0
LABEL_LAYER, LABEL_DT = 101, 0
IO_LABEL_DT = 1
BOUNDARY_LAYER, BOUNDARY_DT = 100, 0


def safe_path(path):
    """Return `path`, or a `_new`-suffixed sibling if `path` is locked (e.g. the
    file is open in Excel / a GDS viewer), so one open file can't abort the run."""
    try:
        if path.exists():
            with open(path, "a"):
                pass
        return path
    except PermissionError:
        alt = path.with_name(path.stem + "_new" + path.suffix)
        print(f"  ! {path.name} is locked (open elsewhere); writing {alt.name} instead.")
        return alt


def metal_layer(k):
    return METAL_BASE + k


def via_layer(k):
    return VIA_BASE + k


COL_PAD, COL_SIGNAL, COL_X, COL_Y = ("pad", "signal", "x (um)", "y (um)")
COL_IO = "i/o"     # grouping column: INPUTn / OUTPUTn (n = group id), blank = unused
_IO_RE = re.compile(r"(INPUT|OUTPUT)\s*(\d+)$")


def classify_io(io_cell):
    """'INPUT3' -> ('input', 3), 'OUTPUT7' -> ('output', 7); else ('', None)."""
    m = _IO_RE.match(io_cell.strip().upper())
    if not m:
        return ("", None)
    return ("input" if m.group(1) == "INPUT" else "output", int(m.group(2)))


# ----------------------------------------------------------------------
# CSV parsing
# ----------------------------------------------------------------------
def read_pads(csv_path):
    with open(csv_path, newline="") as f:
        rows = list(csv.reader(f))
    header_idx = None
    for i, row in enumerate(rows):
        cells = [c.strip().lower() for c in row]
        if COL_X in cells and COL_Y in cells:
            header_idx, header = i, cells
            break
    if header_idx is None:
        raise ValueError(f"No header row with '{COL_X}' and '{COL_Y}'.")
    ix, iy = header.index(COL_X), header.index(COL_Y)
    ipad = header.index(COL_PAD) if COL_PAD in header else None
    isig = header.index(COL_SIGNAL) if COL_SIGNAL in header else None
    iio = header.index(COL_IO) if COL_IO in header else None
    if iio is None:
        raise ValueError(f"No grouping column '{COL_IO}' (INPUTn / OUTPUTn).")
    pads = []
    for row in rows[header_idx + 1:]:
        if len(row) <= max(ix, iy):
            continue
        try:
            x, y = float(row[ix]), float(row[iy])
        except ValueError:
            continue
        name = row[ipad].strip() if ipad is not None and ipad < len(row) else ""
        signal = row[isig].strip() if isig is not None and isig < len(row) else ""
        io_cell = row[iio].strip() if iio < len(row) else ""
        role, group = classify_io(io_cell)
        pads.append({"name": name, "signal": signal, "x": x, "y": y,
                     "io": role, "group": group})
    return pads

def dist(a, b):
    return math.dist((a["x"], a["y"]), (b["x"], b["y"]))

def place_pads(pads, raw_bounds, margins):
    """Position the pad ring in the die so each side is `margins[edge]` from the
    die edge (the die grows by different amounts per side). Die spans x in [0, W],
    y in [0, -H]; the ring's left/top map to margins['left'] / -margins['top']."""
    minx, _, _, maxy = raw_bounds
    ox = margins["left"] - minx
    oy = -margins["top"] - maxy
    for p in pads:
        p["x"] += ox
        p["y"] += oy


def assign_groups(inputs, outputs):
    """Group pads by their IO number, keeping only numbers that have BOTH an input
    and an output. Returns [{"num", "inputs", "outputs"}] sorted by number."""
    in_by, out_by = {}, {}
    for p in inputs:
        in_by.setdefault(p["group"], []).append(p)
    for p in outputs:
        out_by.setdefault(p["group"], []).append(p)
    return [{"num": g, "inputs": in_by[g], "outputs": out_by[g]}
            for g in sorted(set(in_by) & set(out_by))]


def split_coupons(groups, max_in):
    """Turn groups into "coupons" -- the unit that gets one metal layer. A group of
    <= max_in inputs is one coupon; a larger one is split into several independent
    coupons of <= max_in inputs (each carries the group's outputs and restarts the
    resistance ladder, so it measures and decodes on its own). Coupons keep the
    group `num` so sub-coupons of one group can be kept off the same chip."""
    coupons = []
    for g in groups:
        ins = ordered_inputs(g)
        chunks = [ins[i:i + max_in] for i in range(0, len(ins), max_in)] or [[]]
        for j, chunk in enumerate(chunks):
            coupons.append({"num": g["num"], "sub": j, "inputs": chunk,
                            "outputs": g["outputs"]})
    return coupons


def pack_chips(coupons, per_chip):
    """Assign coupons to chips, `per_chip` layers each, never putting two coupons of
    the SAME group on one chip (they share output pads, which would tie them)."""
    if per_chip <= 1:
        return [[c] for c in coupons]
    rem, chips = list(coupons), []
    while rem:
        a = rem.pop(0)
        j = next((i for i, b in enumerate(rem) if b["num"] != a["num"]), None)
        chips.append([a, rem.pop(j)] if j is not None else [a])
    return chips


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------
def polyline_len(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def edge_of(p, bounds):
    """Nearest ring edge a pad sits on."""
    minx, maxx, miny, maxy = bounds
    d = {"left": abs(p["x"] - minx), "right": abs(p["x"] - maxx),
         "bottom": abs(p["y"] - miny), "top": abs(p["y"] - maxy)}
    return min(d, key=d.get)


def inward_along(edge):
    """Return (inward unit vector, along-edge unit vector) for an edge.
    Inward points toward the ring centre; along runs parallel to the edge."""
    return {"bottom": ((0, 1), (1, 0)), "top": ((0, -1), (1, 0)),
            "left": ((1, 0), (0, 1)), "right": ((-1, 0), (0, 1))}[edge]


ROUTE_PITCH = WIRE_WIDTH + WIRE_SPACE   # tooth pitch: adjacent traces sit WIRE_SPACE apart


def input_target_r(k):
    """Target resistance (ohms) for the k-th input of a group (k counts from 1):
    a binary-weighted COIL_BASE_R * 2**(k-1)."""
    return COIL_BASE_R * (2 ** (k - 1))


def input_target_len(k):
    """Trace length (um) that gives the k-th input its target resistance."""
    return input_target_r(k) * WIRE_WIDTH / SHEET_RES


def ordered_inputs(group):
    """Group inputs ranked by distance to the first output (rank k -> target R)."""
    ref = group["outputs"][0]
    return sorted(group["inputs"], key=lambda p: dist(p, ref))


def near_edge_coords(pads, bounds):
    """Per ring edge, the sorted along-edge coordinates of every pad lying ON that
    edge line (within a pad of it) -- so a coil's return can find a real gap to
    thread, including the corner pads that a single-edge classification would miss."""
    minx, maxx, miny, maxy = bounds
    tol = PAD_SIZE
    out = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pads:
        if abs(p["x"] - minx) < tol:
            out["left"].append(p["y"])
        if abs(p["x"] - maxx) < tol:
            out["right"].append(p["y"])
        if abs(p["y"] - miny) < tol:
            out["bottom"].append(p["x"])
        if abs(p["y"] - maxy) < tol:
            out["top"].append(p["x"])
    for e in out:
        out[e].sort()
    return out


CORNER_INSET = PAD_SIZE + WIRE_SPACE             # keep combs clear of the ring corners
LEAD_RADIUS = PAD_SIZE / 2.0 + COIL_GAP / 2.0    # pad->comb lead lane (under the teeth)


def to_xy(e, t, r, bounds):
    """Point at along-edge coordinate `t` and outward radius `r` (measured from the
    ring edge line) on edge `e`. r > 0 is OUTSIDE the ring, where the comb sits;
    r < 0 is inside, toward the central plane."""
    minx, maxx, miny, maxy = bounds
    if e == "left":
        return (minx - r, t)
    if e == "right":
        return (maxx + r, t)
    if e == "bottom":
        return (t, miny - r)
    return (t, maxy + r)                                  # top


def edge_along_range(e, bounds):
    """Usable along-edge interval for edge `e`, inset from the ring corners so combs
    on adjacent edges can't collide at a corner."""
    minx, maxx, miny, maxy = bounds
    lo, hi = (miny, maxy) if e in ("left", "right") else (minx, maxx)
    return lo + CORNER_INSET, hi - CORNER_INSET


def edge_gaps(e, edge_coords):
    """Along-edge centres of the pad-to-pad gaps on edge `e` -- a comb's return drops
    inward through one of these, threading between pads without touching them."""
    c = edge_coords[e]
    return [(c[i] + c[i + 1]) / 2.0 for i in range(len(c) - 1)]


def edge_inputs(group, bounds):
    """Same-group inputs bucketed by ring edge, each list sorted by along-coordinate
    and tagged (along, target_len, pad); target_len comes from the ladder rank."""
    ranks = {id(p): k for k, p in enumerate(ordered_inputs(group), 1)}
    by_edge = {}
    for p in group["inputs"]:
        e = edge_of(p, bounds)
        _, v = inward_along(e)
        a = p["x"] * v[0] + p["y"] * v[1]
        by_edge.setdefault(e, []).append((a, input_target_len(ranks[id(p)]), p))
    for e in by_edge:
        by_edge[e].sort(key=lambda it: it[0])
    return by_edge


def solve_bands(items, edge_lo, edge_hi):
    """Partition [edge_lo, edge_hi] into one contiguous along-edge BAND per input
    (kept in pad order) so that (a) every band strictly contains its own pad and
    (b) band widths are as close to proportional to each input's trace length as the
    pad positions allow. A comb folded to fill its band has depth = len*pitch/width,
    so length-proportional widths LEVEL the depth across the edge -- making the
    deepest comb, which sets the die size, as shallow as the pads permit. Because the
    bands are disjoint and each holds its pad, every comb stays in its own band and
    no two combs can ever touch. Returns [(lo, hi)] aligned with `items`."""
    n = len(items)
    a = [it[0] for it in items]
    if n == 1:
        return [(edge_lo, edge_hi)]
    need = [it[1] * ROUTE_PITCH for it in items]          # wire area = width * depth
    eps = WIRE_SPACE + WIRE_WIDTH                          # keep neighbouring combs apart

    def cuts_for(depth):
        """Band boundaries giving every comb width >= need/depth (depth <= `depth`),
        each band still holding its pad; None if the pad spacing makes it impossible."""
        cuts, prev = [], edge_lo
        for i in range(n - 1):
            c = min(max(prev + need[i] / depth, a[i] + eps), a[i + 1] - eps)
            if c - prev < need[i] / depth - 1e-6 or not prev <= a[i] <= c:
                return None
            cuts.append(c)
            prev = c
        if edge_hi - prev < need[-1] / depth - 1e-6 or not prev <= a[-1] <= edge_hi:
            return None
        return cuts

    hi = max(need)                          # width >= ~1 um per comb: surely feasible
    while cuts_for(hi) is None:
        hi *= 2.0
    lo = 1e-9
    for _ in range(50):                     # smallest feasible (uniform) depth
        mid = (lo + hi) / 2.0
        if cuts_for(mid) is None:
            lo = mid
        else:
            hi = mid
    cuts = cuts_for(hi) or [(a[i] + a[i + 1]) / 2.0 for i in range(n - 1)]
    edges = [edge_lo] + cuts + [edge_hi]
    return [(edges[i], edges[i + 1]) for i in range(n)]


def return_through_gap(px, py, far_end, cu, plane):
    """Connection from the comb's far end straight INWARD onto the central plane.
    The far end already sits in a pad-to-pad gap (see comb_plan), so this drops
    perpendicular through that gap without crossing any comb. Returns the polyline."""
    px0, py0, px1, py1 = plane
    u = (-cu[0], -cu[1])                                   # inward
    if u == (0, 1):                                        # bottom edge -> plane bottom
        land = (far_end[0], py0)
    elif u == (0, -1):                                     # top
        land = (far_end[0], py1)
    elif u == (1, 0):                                      # left
        land = (px0, far_end[1])
    else:                                                  # right
        land = (px1, far_end[1])
    into = (land[0] + u[0] * FINGER_OVERLAP, land[1] + u[1] * FINGER_OVERLAP)
    return [far_end, land, into]


# A right-angle bend conducts ~CORNER_SQUARES squares, not the ~1.0 that counting the
# centre-line length through the corner assigns it (current crowds to the inside of the
# turn). A wide-shallow comb has ~2 bends per tooth, so this is a real correction.
CORNER_SQUARES = 0.56


def _overlap(a0, a1, b0, b1):
    lo, hi = max(min(a0, a1), min(b0, b1)), min(max(a0, a1), max(b0, b1))
    return max(0.0, hi - lo)


def _seg_outside(p, q, nodes):
    """Length of axis-aligned segment p->q lying OUTSIDE every (equipotential) node
    rectangle. Nodes here (pad, plane) don't overlap each other, so the max covered
    length is the covered length."""
    length = math.dist(p, q)
    if length < 1e-9:
        return 0.0
    horiz, vert = abs(p[1] - q[1]) < 1e-6, abs(p[0] - q[0]) < 1e-6
    if not (horiz or vert):
        return length                                  # no diagonal traces in practice
    covered = 0.0
    for rx0, ry0, rx1, ry1 in nodes:
        if horiz and ry0 - 1e-6 <= p[1] <= ry1 + 1e-6:
            covered = max(covered, _overlap(p[0], q[0], rx0, rx1))
        elif vert and rx0 - 1e-6 <= p[0] <= rx1 + 1e-6:
            covered = max(covered, _overlap(p[1], q[1], ry0, ry1))
    return max(0.0, length - covered)


def _in_node(p, nodes):
    return any(rx0 - 1e-6 <= p[0] <= rx1 + 1e-6 and ry0 - 1e-6 <= p[1] <= ry1 + 1e-6
              for rx0, ry0, rx1, ry1 in nodes)


def _is_corner(a, b, c):
    d1, d2 = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    return (abs(d1[0] * d2[0] + d1[1] * d2[1]) < 1e-6
            and (d1[0] or d1[1]) and (d2[0] or d2[1]))


def resistive_length(pts, nodes):
    """Effective resistive trace length (um) of an axis-aligned polyline: the
    centre-line length lying OUTSIDE the equipotential `nodes` (the wide pad and plane
    metal carry ~0 ohm), minus a per-bend correction (a right-angle corner conducts
    ~CORNER_SQUARES squares, not the ~1 a straight square count gives it). The real
    resistance is then SHEET_RES * resistive_length / WIRE_WIDTH -- i.e. this counts
    the actual conducting metal, including the lead and return, not just the nominal
    coil length."""
    clean = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - clean[-1][0]) > 1e-9 or abs(p[1] - clean[-1][1]) > 1e-9:
            clean.append(p)
    straight = sum(_seg_outside(clean[i], clean[i + 1], nodes)
                   for i in range(len(clean) - 1))
    bends = sum(1 for i in range(1, len(clean) - 1)
                if _is_corner(clean[i - 1], clean[i], clean[i + 1])
                and not _in_node(clean[i], nodes))
    return max(0.0, straight - (1.0 - CORNER_SQUARES) * WIRE_WIDTH * bends)


def build_band_coil(pad, e, band, target_len, gaps, bounds, plane):
    """Fold one input's resistor into a compact radial-teeth comb that FILLS its
    along-edge `band`, so it is wide and shallow, with the tooth depth solved so the
    total trace length -- lead + teeth + return -- equals `target_len` (length is
    linear in depth, so a few iterations nail it). A short lead joins the pad to the
    near end of the band; the comb runs to the far end, whose last tooth is parked on
    a pad gap so the return drops straight inward to the plane. Every point stays
    within the band, so combs of different inputs can never touch. Returns
    (coil_pts, return_pts, R_ohm, total_length) -- R_ohm is the ACTUAL extracted
    resistance (corner- and pad/plane-corrected, see resistive_length), which differs
    from SHEET*total_length/W because of the bends and the wide-metal overlaps."""
    lo, hi = band
    u, v = inward_along(e)
    cu = (-u[0], -u[1])                                    # outward, away from the ring
    a_pad = pad["x"] * v[0] + pad["y"] * v[1]
    r0 = PAD_SIZE / 2.0 + COIL_GAP
    blo = lo + (WIRE_SPACE + WIRE_WIDTH) / 2.0
    bhi = hi - (WIRE_SPACE + WIRE_WIDTH) / 2.0
    # Tooth count: as many as the band can hold (wide, shallow), but no more than the
    # length warrants -- so a short resistor stays compact near its pad instead of
    # being stretched thin across a big band (which would overshoot its target).
    fit = max(2, int((bhi - blo) / ROUTE_PITCH) // 2 * 2)
    cap = max(2, int(target_len / (4.0 * ROUTE_PITCH)) // 2 * 2)   # depth >= ~4 pitches
    in_band = [g for g in gaps if blo <= g <= bhi]
    # Park the comb in the band so its FAR end (the return) lands on a pad gap and its
    # NEAR end (the lead) is as close to the pad as possible. Use as many teeth as the
    # band holds (wide, shallow) but no more than the length warrants; if a near
    # full-width comb leaves no slack to reach a gap, drop teeth until one is reachable
    # -- so the comb (and its return) always stays inside the band.
    m, t0, s, far_gap = min(fit, cap), None, None, None
    while m >= 2:
        reach = (m - 1) * ROUTE_PITCH
        cands = []
        for g in in_band:
            if g - reach >= blo - 1e-6:                    # g = far (high) end
                cands.append((abs(a_pad - (g - reach)), g - reach, 1.0, g))
            if g + reach <= bhi + 1e-6:                    # g = far (low) end
                cands.append((abs(a_pad - (g + reach)), g + reach, -1.0, g))
        if cands:
            _, t0, s, far_gap = min(cands, key=lambda c: c[0])
            break
        m -= 2
    if t0 is None:                                         # band holds no gap: best-effort
        m, reach, s = 2, ROUTE_PITCH, 1.0
        t0 = max(blo, min(a_pad, bhi - reach))
        far_gap = t0 + s * reach

    def build(depth):
        P = lambda t, r: to_xy(e, t, r, bounds)
        pts = [(pad["x"], pad["y"]), P(a_pad, LEAD_RADIUS),
               P(t0, LEAD_RADIUS), P(t0, r0)]              # pad -> lead lane -> tooth 0
        out = True
        for j in range(m):
            t = t0 + s * j * ROUTE_PITCH
            if out:                                        # tooth in -> out
                pts += [P(t, r0), P(t, r0 + depth)]
                if j < m - 1:
                    pts.append(P(t + s * ROUTE_PITCH, r0 + depth))
            else:                                          # tooth out -> in
                pts += [P(t, r0 + depth), P(t, r0)]
                if j < m - 1:
                    pts.append(P(t + s * ROUTE_PITCH, r0))
            out = not out
        ret = return_through_gap(0.0, 0.0, P(far_gap, r0), cu, plane)
        return pts, ret

    depth, coil, ret, clen = target_len / m, None, None, 0.0
    for _ in range(3):
        coil, ret = build(depth)
        clen = polyline_len(coil) + polyline_len(ret)
        depth = max(ROUTE_PITCH, depth + (target_len - clen) / m)
    hp = PAD_SIZE / 2.0                                    # actual resistance of the
    nodes = [(pad["x"] - hp, pad["y"] - hp, pad["x"] + hp, pad["y"] + hp), plane]
    r_ohm = SHEET_RES * resistive_length(coil + ret, nodes) / WIRE_WIDTH
    return coil, ret, r_ohm, clen


# ----------------------------------------------------------------------
# Draw one group on one layer
# ----------------------------------------------------------------------
def rect(x0, y0, x1, y1, layer):
    return gdstk.rectangle((x0, y0), (x1, y1), layer=layer, datatype=METAL_DT)


def pad_poly(p, layer):
    h = PAD_SIZE / 2.0
    return rect(p["x"] - h, p["y"] - h, p["x"] + h, p["y"] + h, layer)


def via_polys(pt, k):
    """Via stack connecting metal k and metal k+1 at pt (via cut + both metals)."""
    h = VIA_SIZE / 2.0
    x, y = pt
    return [gdstk.rectangle((x - h, y - h), (x + h, y + h), layer=Lr, datatype=d)
            for Lr, d in ((via_layer(k), VIA_DT),
                          (metal_layer(k), METAL_DT),
                          (metal_layer(k + 1), METAL_DT))]


def central_plane(bounds):
    """The central conductive PLANE: it fills essentially the whole interior of the
    ring (the combs all sit outside), inset only by PLANE_RING_MARGIN so it clears
    the pads. Returns (px0, py0, px1, py1)."""
    minx, maxx, miny, maxy = bounds
    return (minx + PLANE_RING_MARGIN, miny + PLANE_RING_MARGIN,
            maxx - PLANE_RING_MARGIN, maxy - PLANE_RING_MARGIN)


def draw_group(group, layer_idx, bounds, cell, chip_idx, all_pads):
    """Build one group on metal layer `layer_idx`: a big central PLANE fills the
    interior as the shared node, and every INPUT grows a resistor COMB on the
    OUTSIDE of the pad ring, folded to fill its own along-edge BAND (width allocated
    proportional to its trace length) so it stays wide and shallow and the deepest
    comb -- which sets the die size -- is as shallow as the pads allow. The comb's
    far end returns INWARD through a pad gap onto the plane, so the resistor body
    sits outside and only a thin wire threads between the pads. Each OUTPUT joins the
    plane with a pad-width finger. Pads are drawn separately on the top layer; for a
    2nd-layer group a via at each pad centre drops the metal up to the top-layer pad.
    Returns (wiring_polys, csv_rows)."""
    L = metal_layer(layer_idx)
    needs_via = layer_idx > 0
    hp = PAD_SIZE / 2.0
    polys = []

    def add(obj):
        cell.add(obj)
        polys.extend(obj.to_polygons() if isinstance(obj, gdstk.FlexPath) else [obj])

    def via_at(pt):
        for vp in via_polys(pt, 0):                 # stitch metal L down to top
            add(vp)

    px0, py0, px1, py1 = central_plane(bounds)
    add(rect(px0, py0, px1, py1, L))                 # the big conductive plane
    plane = (px0, py0, px1, py1)
    edge_coords = near_edge_coords(all_pads, bounds)

    # Allocate each input a disjoint along-edge band (width proportional to its trace
    # length) so its comb stays wide and shallow; geometry is solved per band below.
    band_of, gaps_of = {}, {}
    for e, items in edge_inputs(group, bounds).items():
        gaps_of[e] = edge_gaps(e, edge_coords)
        elo, ehi = edge_along_range(e, bounds)
        for (a, tl, pad), band in zip(items, solve_bands(items, elo, ehi)):
            band_of[id(pad)] = (e, band)

    rows, r_vals = [], []
    for k, inp in enumerate(ordered_inputs(group), 1):
        target_len = input_target_len(k)
        e, band = band_of[id(inp)]
        coil, ret, r, clen = build_band_coil(inp, e, band, target_len,
                                             gaps_of[e], bounds, plane)
        add(gdstk.FlexPath(coil, WIRE_WIDTH, layer=L, datatype=METAL_DT))
        add(gdstk.FlexPath(ret, WIRE_WIDTH, layer=L, datatype=METAL_DT))
        if needs_via:
            via_at((inp["x"], inp["y"]))
        r_vals.append(r)
        rows.append([chip_idx, layer_idx + 1, inp["name"],
                     inp["signal"], ";".join(o["name"] for o in group["outputs"]),
                     f"{r:.2f}", f"{clen:.0f}"])

    for o in group["outputs"]:
        e = edge_of(o, bounds)
        cx, cy = o["x"], o["y"]
        if e in ("bottom", "top"):
            fx0, fx1 = cx - hp, cx + hp
            if e == "bottom":
                add(rect(fx0, cy, fx1, py0 + FINGER_OVERLAP, L))
            else:
                add(rect(fx0, py1 - FINGER_OVERLAP, fx1, cy, L))
        else:
            fy0, fy1 = cy - hp, cy + hp
            if e == "left":
                add(rect(cx, fy0, px0 + FINGER_OVERLAP, fy1, L))
            else:
                add(rect(px1 - FINGER_OVERLAP, fy0, cx, fy1, L))
        if needs_via:
            via_at((cx, cy))

    # All-contacting parallel resistance of the group (what the tester reads with
    # every input probe landed); recorded on every row of the group.
    gpar = 1.0 / sum(1.0 / r for r in r_vals) if r_vals else float("inf")
    for row in rows:
        row.append(f"{gpar:.3f}")
    return polys, rows


# ----------------------------------------------------------------------
# Verify (cross-group overlap on a shared layer -- should be none)
# ----------------------------------------------------------------------
def group_bbox(polys):
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for p in polys:
        bb = p.bounding_box()
        if bb is None:
            continue
        (a, b), (c, d) = bb
        xmin, ymin = min(xmin, a), min(ymin, b)
        xmax, ymax = max(xmax, c), max(ymax, d)
    return xmin, ymin, xmax, ymax


def find_shorts(geo, num_layers):
    layers = tuple(metal_layer(k) for k in range(num_layers))
    ids = list(geo.keys())
    bbox = {nid: group_bbox(polys) for nid, polys in geo.items()}
    by_layer = {(nid, L): [p for p in polys if p.layer == L]
                for nid, polys in geo.items() for L in layers}
    shorts = []
    for a in range(len(ids)):
        xa0, ya0, xa1, ya1 = bbox[ids[a]]
        for b in range(a + 1, len(ids)):
            xb0, yb0, xb1, yb1 = bbox[ids[b]]
            if xa1 < xb0 or xb1 < xa0 or ya1 < yb0 or yb1 < ya0:
                continue
            for L in layers:
                pa, pb = by_layer[(ids[a], L)], by_layer[(ids[b], L)]
                if pa and pb and gdstk.boolean(pa, pb, "and"):
                    shorts.append((ids[a], ids[b], L))
    return shorts


# ----------------------------------------------------------------------
# Calibration coupon (human multimeter measurement)
# ----------------------------------------------------------------------
def square_meander(x_left, top_y, target_len):
    """Compact, roughly-square boustrophedon resistor of `target_len` um of trace,
    starting at (x_left, top_y) and growing DOWNWARD. Returns (pts, width, height)
    where pts is the centre-line polyline (top end first, bottom end last)."""
    pitch = ROUTE_PITCH
    rows = max(1, round(math.sqrt(max(target_len, 1.0) * pitch) / pitch))
    w = max(WIRE_WIDTH, target_len / rows)               # run width -> ~square block
    pts = [(x_left, top_y)]
    y = top_y
    for r in range(rows):
        x_to = x_left + w if r % 2 == 0 else x_left
        pts.append((x_to, y))                            # horizontal run
        y -= pitch
        pts.append((x_to, y))                            # step down one pitch
    return pts, w, top_y - y


def build_calibration(resistances):
    """One big COMMON pad joined through a known meander resistor to each of several
    big probe pads (all on metal 1), each labelled with its theoretical resistance
    and trace length. The coupon uses the SAME die size as the test chips
    (DIE_W x DIE_H, set in size_and_place) with its content centred; it only grows
    past that if its own content would not otherwise fit. Returns
    (library, csv_rows, die_w, die_h)."""
    L = metal_layer(0)
    blocks = []
    for R in resistances:
        target = R * WIRE_WIDTH / SHEET_RES
        _, w, h = square_meander(0.0, 0.0, target)
        blocks.append({"R": R, "len": target, "w": w, "h": h})
    hmax = max(b["h"] for b in blocks)
    col_w = [max(b["w"], CALIB_PAD) for b in blocks]
    total_w = sum(col_w) + CALIB_GAP * (len(blocks) - 1)
    band_h = CALIB_PAD + CALIB_GAP + hmax + CALIB_GAP + CALIB_PAD + 2 * CALIB_LABEL
    # Match the test-chip die; only enlarge if the content would not fit inside it.
    content_w, content_h = total_w + 2 * CALIB_GAP, band_h + 2 * CALIB_GAP
    die_w, die_h = max(DIE_W, content_w), max(DIE_H, content_h)

    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell("CALIB")
    x0 = (die_w - total_w) / 2.0            # centre the column band horizontally
    bus_top = -(die_h - band_h) / 2.0       # centre the content band vertically
    bus_bot = bus_top - CALIB_PAD
    mnd_top = bus_bot - CALIB_GAP
    pad_top = mnd_top - hmax - CALIB_GAP
    pad_bot = pad_top - CALIB_PAD

    cell.add(rect(x0, bus_bot, x0 + total_w, bus_top, L))   # the common bus pad
    if ADD_LABELS:
        cell.add(*gdstk.text("COMMON", CALIB_LABEL, (x0 + 20, bus_top + 20),
                             layer=LABEL_LAYER, datatype=LABEL_DT))

    rows, x = [], x0
    for b, cw in zip(blocks, col_w):
        cx = x + cw / 2.0
        target = b["len"]                          # full COMMON -> pad trace length
        # Solve the meander length so the WHOLE trace (bus stub + meander + L to the
        # pad) equals `target`, i.e. the resistor really is b["R"] -- not just the
        # meander. Length is ~linear in the meander, so a few steps converge.
        mlen, pts = target, None
        for _ in range(4):
            _, w, _ = square_meander(0.0, 0.0, mlen)
            xl = cx - w / 2.0
            mpts, _, _ = square_meander(xl, mnd_top, mlen)
            pts = [(xl, bus_bot)] + mpts                    # stub up into the bus
            xe = pts[-1][0]
            pts += [(xe, pad_top), (cx, pad_top)]           # L down to the probe pad
            mlen += target - polyline_len(pts)
        cell.add(gdstk.FlexPath(pts, WIRE_WIDTH, layer=L, datatype=METAL_DT))
        cell.add(rect(cx - CALIB_PAD / 2.0, pad_bot, cx + CALIB_PAD / 2.0, pad_top, L))

        length = polyline_len(pts)
        squares = length / WIRE_WIDTH
        theo_r = SHEET_RES * squares                # == b["R"] after the solve
        if ADD_LABELS:
            cell.add(*gdstk.text(f"{theo_r:.0f} ohm", CALIB_LABEL,
                                 (cx - CALIB_PAD / 2.0, pad_bot - CALIB_LABEL - 15),
                                 layer=LABEL_LAYER, datatype=LABEL_DT))
            cell.add(*gdstk.text(f"{length:.0f} um", CALIB_LABEL * 0.7,
                                 (cx - CALIB_PAD / 2.0, pad_bot - 2 * CALIB_LABEL - 30),
                                 layer=LABEL_LAYER, datatype=IO_LABEL_DT))
        rows.append([f"{b['R']:.0f}", f"{theo_r:.1f}", f"{squares:.1f}", f"{length:.0f}"])
        x += cw + CALIB_GAP
    if ADD_DIE_OUTLINE:                          # bottom layer, built LAST
        cell.add(gdstk.rectangle((0.0, 0.0), (die_w, -die_h),
                                 layer=BOUNDARY_LAYER, datatype=BOUNDARY_DT))
    return lib, rows, die_w, die_h


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def parse_args(argv):
    """Returns (csv_path, layers). `--layers 1|2` skips the prompt; otherwise
    layers is None and main() asks."""
    layers, pos = None, []
    i = 1
    while i < len(argv):
        if argv[i] in ("--layers", "-l") and i + 1 < len(argv):
            layers = argv[i + 1]
            i += 2
        else:
            pos.append(argv[i])
            i += 1
    csv_path = pathlib.Path(pos[0]) if pos else INPUT_CSV
    return csv_path, (int(layers) if layers in ("1", "2") else None)


def ask_layers(default=2):
    """Ask whether to build 1- or 2-layer chips. 1 layer -> ONE IO group per
    chip (all metal 1, no vias); 2 layers -> two IO groups per chip (2nd group
    on metal 2, via-stitched to its pads). Non-interactive runs use `default`."""
    if not sys.stdin.isatty():
        return default
    prompt = ("\nBuild chips with how many metal layers?\n"
              "  1 = one layer  (one IO group per chip, no vias)\n"
              "  2 = two layers (two IO groups per chip)\n"
              f"Enter 1 or 2 [{default}]: ")
    while True:
        try:
            ans = input(prompt).strip()
        except EOFError:
            return default
        if ans == "":
            return default
        if ans in ("1", "2"):
            return int(ans)
        print("  Please enter 1 or 2.")


def size_and_place(pads, groups):
    """Build every coil at its raw position to measure its TRUE protrusion past the
    ring on each edge, set the global die size (proportional to the pad ring, just
    large enough to clear the farthest comb by DIE_MARGIN_BUFFER), and position the
    ring centred inside it. Returns (edge_depth, ring_w, ring_h, scale)."""
    rxs = [p["x"] for p in pads]
    rys = [p["y"] for p in pads]
    raw_bounds = (min(rxs), max(rxs), min(rys), max(rys))
    raw_edges = near_edge_coords(pads, raw_bounds)
    raw_plane = central_plane(raw_bounds)
    ring_w, ring_h = max(rxs) - min(rxs), max(rys) - min(rys)
    edge_depth = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
    for g in groups:
        for e, items in edge_inputs(g, raw_bounds).items():
            gaps = edge_gaps(e, raw_edges)
            elo, ehi = edge_along_range(e, raw_bounds)
            for (a, tl, pad), band in zip(items, solve_bands(items, elo, ehi)):
                coil, _, _, _ = build_band_coil(pad, e, band, tl, gaps,
                                                raw_bounds, raw_plane)
                xs = [p[0] for p in coil]
                ys = [p[1] for p in coil]
                prot = {"left": raw_bounds[0] - min(xs), "right": max(xs) - raw_bounds[1],
                        "bottom": raw_bounds[2] - min(ys), "top": max(ys) - raw_bounds[3]}[e]
                edge_depth[e] = max(edge_depth[e], prot + WIRE_WIDTH / 2.0)  # trace edge

    buf = DIE_MARGIN_BUFFER
    content_w = ring_w + edge_depth["left"] + edge_depth["right"] + 2 * buf
    content_h = ring_h + edge_depth["top"] + edge_depth["bottom"] + 2 * buf
    scale = max(content_w / ring_w, content_h / ring_h)
    global DIE_W, DIE_H
    DIE_W, DIE_H = scale * ring_w, scale * ring_h
    margins = {                                 # centre the content in the die
        "left": edge_depth["left"] + buf + (DIE_W - content_w) / 2,
        "right": edge_depth["right"] + buf + (DIE_W - content_w) / 2,
        "top": edge_depth["top"] + buf + (DIE_H - content_h) / 2,
        "bottom": edge_depth["bottom"] + buf + (DIE_H - content_h) / 2,
    }
    place_pads(pads, raw_bounds, margins)
    return edge_depth, ring_w, ring_h, scale


def build_chip(ci, chip_groups, pads, bounds):
    """Build one chip's GDS: every pad on the top layer (wired ones tagged to their
    group, the rest inert), each group's combs on its own metal layer, then the die
    outline LAST. Returns (library, geo, csv_rows); geo maps net-id -> polygons."""
    top = metal_layer(0)
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell(f"CHIP{ci}")
    geo = {}
    pad_net = {}
    for group in chip_groups:
        for p in group["inputs"] + group["outputs"]:
            pad_net[id(p)] = ("group", group["num"])
    for p in pads:
        key = pad_net.get(id(p), ("pads",))
        poly = pad_poly(p, top)
        cell.add(poly)
        geo.setdefault(key, []).append(poly)
        if ADD_LABELS and p["name"]:
            inset = LABEL_SIZE * 0.4                  # tuck labels inside the pad
            lx = p["x"] - PAD_SIZE / 2 + inset
            ty = p["y"] + PAD_SIZE / 2 - inset - LABEL_SIZE  # top text line, inside
            if p["io"]:                               # IO tag on top, pad name below
                cell.add(*gdstk.text(p["io"].capitalize(), LABEL_SIZE, (lx, ty),
                                     layer=LABEL_LAYER, datatype=IO_LABEL_DT))
                cell.add(*gdstk.text(p["name"], LABEL_SIZE, (lx, ty - LABEL_SIZE * 1.2),
                                     layer=LABEL_LAYER, datatype=LABEL_DT))
            else:
                cell.add(*gdstk.text(p["name"], LABEL_SIZE, (lx, ty),
                                     layer=LABEL_LAYER, datatype=LABEL_DT))
    rows = []
    for li, group in enumerate(chip_groups):
        wpolys, grows = draw_group(group, li, bounds, cell, ci, pads)
        geo.setdefault(("group", group["num"]), []).extend(wpolys)
        rows.extend(grows)
    if ADD_DIE_OUTLINE:                          # bottom layer, built LAST
        cell.add(gdstk.rectangle((0.0, 0.0), (DIE_W, -DIE_H),
                                 layer=BOUNDARY_LAYER, datatype=BOUNDARY_DT))
    return lib, geo, rows


def main():
    csv_path, layers = parse_args(sys.argv)
    if layers is None:
        layers = ask_layers()
    groups_per_chip = layers
    pads = read_pads(csv_path)
    inputs = [p for p in pads if p["io"] == "input"]
    outputs = [p for p in pads if p["io"] == "output"]
    if not inputs or not outputs:
        raise SystemExit("Need at least one input and one output.")
    groups = assign_groups(inputs, outputs)
    # Big groups are split into independent <= MAX_BINARY_INPUTS sub-coupons so each
    # coupon's resistance range -- and its parallel-decode margin -- stays sane.
    coupons = split_coupons(groups, MAX_BINARY_INPUTS)

    # Size the die from the real coil protrusions and place the ring (see the
    # bottom-layer rules in size_and_place).
    edge_depth, ring_w, ring_h, scale = size_and_place(pads, coupons)

    print(f"{len(inputs)} inputs, {len(outputs)} outputs; die {DIE_W:.0f} x {DIE_H:.0f} um ")
    print("Coil protrusion per edge (um): " +
          ", ".join(f"{e} {edge_depth[e]:.0f}" for e in ("left", "right", "top", "bottom")))
    print(f"Aluminium {METAL_THICKNESS_UM} um thick, W={WIRE_WIDTH} um -> "
          f"sheet res {SHEET_RES:.3f} ohm/sq; {WIRE_WIDTH/SHEET_RES:.1f} um per ohm.")
    big = max((len(c["inputs"]) for c in coupons), default=0)
    if input_target_r(big) > 1e6 or max(DIE_W, DIE_H) > 5e4:
        print(f"  ! BINARY ladder still needs a {input_target_r(big):,.0f} ohm "
              f"resistor (a {big}-input coupon) and a {DIE_W/1000:.0f} x "
              f"{DIE_H/1000:.0f} mm die -- lower MAX_BINARY_INPUTS.")
    matched = [g["num"] for g in groups]
    wired_in = sum(len(g["inputs"]) for g in groups)
    print(f"Matched group number(s) {matched}: {len(groups)} group(s), "
          f"{wired_in} input(s) wired (unmatched numbers dropped).")
    if len(coupons) > len(groups):
        print(f"  Split into {len(coupons)} layers"
              f"(<= {MAX_BINARY_INPUTS} inputs per layer).")
    for g in groups:
        nsub = sum(1 for c in coupons if c["num"] == g["num"])
        extra = f" -> {nsub} layers total" if nsub > 1 else ""
        print(f"  group {g['num']}: {len(g['inputs'])} input(s), "
              f"{len(g['outputs'])} output(s){extra}")

    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    bounds = (min(xs), max(xs), min(ys), max(ys))
    OUTPUT_DIR.mkdir(exist_ok=True)

    chips = pack_chips(coupons, groups_per_chip)
    print(f"{layers}-layer chips: up to {groups_per_chip} layer(s) per chip "
          f"-> {len(chips)} chip(s).")
    # Name each GDS by the group(s) it holds; show the sub-index only for groups
    # that were split into several coupons (e.g. group 3 -> "3.0", "3.1").
    nsub = {}
    for c in coupons:
        nsub[c["num"]] = nsub.get(c["num"], 0) + 1
    tag = lambda c: f"{c['num']}.{c['sub']}" if nsub[c["num"]] > 1 else f"{c['num']}"
    all_rows = []
    for ci, chip_coupons in enumerate(chips, 1):
        lib, geo, rows = build_chip(ci, chip_coupons, pads, bounds)
        all_rows.extend(rows)
        shorts = find_shorts(geo, len(chip_coupons))
        tags = [tag(c) for c in chip_coupons]
        gds_out = safe_path(OUTPUT_DIR / f"groups_{'_'.join(tags)}.gds")
        lib.write_gds(gds_out)
        mlayers = [metal_layer(li) for li in range(len(chip_coupons))]
        # print(f"  chip {ci}/{len(chips)}: group(s) {tags} on metal layer(s) "
        #       f"{mlayers}, {len(shorts)} cross-net overlap(s) -> wrote {gds_out.name}")

    csv_out = safe_path(OUTPUT_DIR / f"{csv_path.stem}_parallel.csv")
    with open(csv_out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chip", "layer", "input_pad", "input_signal",
                    "output_pads", "actual_R_ohm", "total_len_um",
                    "group_parallel_R_ohm"])
        w.writerows(all_rows)
    print(f"Wrote {csv_out} ({len(all_rows)} coils across {len(chips)} chip(s)).")

    # Calibration coupon (self-sized; resistances match the on-chip ladder).
    calib_res = [input_target_r(k) for k in range(1, CALIB_COUNT + 1)]
    calib_lib, calib_rows, cdw, cdh = build_calibration(calib_res)
    calib_gds = safe_path(OUTPUT_DIR / "calibration_resistors.gds")
    calib_lib.write_gds(calib_gds)
    calib_csv = safe_path(OUTPUT_DIR / "calibration_resistors.csv")
    with open(calib_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["target_R_ohm", "theoretical_R_ohm", "squares", "trace_len_um"])
        w.writerows(calib_rows)
    fit = "" if (cdw, cdh) == (DIE_W, DIE_H) else " (enlarged to fit its content)"
    print(f"Wrote {calib_gds.name}: {cdw/1000:.1f}x{cdh/1000:.1f} mm die "
          f"matching the test chips{fit}, COMMON "
          f"pad + {len(calib_rows)} probe pads "
          f"({', '.join(r[0] + ' ohm' for r in calib_rows)}); measure each vs COMMON, "
          f"then actual sheet res = R_measured / squares.")


if __name__ == "__main__":
    main()
