import csv
import sys
import math
import heapq
import pathlib

import gdstk

HERE = pathlib.Path(__file__).parent
INPUT_CSV = HERE / "inputs" / "pinout.csv"
OUTPUT_DIR = HERE / "outputs"

PAD_SIZE = 80.0
WIRE_WIDTH = 10.0
CLEARANCE = 15.0            # min gap between a wire and a foreign pad/wire
VIA_SIZE = 12.0

GRID = 50.0                 # router grid pitch
VIA_COST = 20.0             # discourages layer changes (keeps via count down)

ADD_LABELS = True
LABEL_SIZE = 12.0

ADD_DIE_OUTLINE = True
DIE_W, DIE_H = 8213.0, 5199.0   # origin = top-left, +X right, -Y down (assumed)

MIN_LAYERS = 1                    # start here and add layers only as needed
MAX_LAYERS = 2                    # give up (best effort) beyond this

# Layers / datatypes  ---  PLACEHOLDERS: set to the fab's layer table.
METAL_BASE, METAL_DT = 1, 0      
VIA_BASE, VIA_DT = 20, 0        
LABEL_LAYER, LABEL_DT = 101, 0   
IO_LABEL_DT = 1                   # input/output role text (same layer, datatype 1)
BOUNDARY_LAYER, BOUNDARY_DT = 100, 0

def metal_layer(k):
    return METAL_BASE + k

def via_layer(k):                 # via connecting metal k and metal k+1
    return VIA_BASE + k

COL_PAD, COL_SIGNAL, COL_X, COL_Y = ("pad", "signal", "x (um)", "y (um)")

INPUT_SIGNAL_PREFIXES = ("VDD", "VSS")     # power/ground pads -> inputs
OUTPUT_SIGNAL_PREFIXES = ("VATB",)         # analog test-bus pads -> outputs
OUTPUT_SIGNAL_SUFFIXES = ("_SNS",)         # sense pads -> outputs

def classify_io(signal):
    s = signal.strip().upper()
    # Output rules take precedence (e.g. VDD08PA_SNS is a sense pad -> output).
    if s.startswith(OUTPUT_SIGNAL_PREFIXES) or s.endswith(OUTPUT_SIGNAL_SUFFIXES):
        return "output"
    if s.startswith(INPUT_SIGNAL_PREFIXES):
        return "input"
    return ""

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
        io = classify_io(signal)
        pads.append({"name": name, "signal": signal, "x": x, "y": y, "io": io})
    return pads

def dist(a, b): #distance
    return math.dist((a["x"], a["y"]), (b["x"], b["y"]))

def assign_outputs(inputs, outputs):
    cap = math.ceil(len(outputs) / len(inputs)) # evenly distributes outputs to inputs
    load = {id(i): 0 for i in inputs}
    nets = {id(i): {"input": i, "outputs": []} for i in inputs}
    for o in sorted(outputs, key=lambda p: (p["x"], p["y"])):
        for i in sorted(inputs, key=lambda i: dist(i, o)):
            if load[id(i)] < cap:
                nets[id(i)]["outputs"].append(o)
                load[id(i)] += 1
                break
    return [nets[id(i)] for i in inputs]

def wire_router_grid(p):
    return (round(p["x"] / GRID), round(-p["y"] / GRID))

def grid_to_coordinates(cell):
    return (cell[0] * GRID, -cell[1] * GRID)

def build_cover(pads):
    keep_out_radius = PAD_SIZE / 2.0 + CLEARANCE + WIRE_WIDTH / 2.0
    cover = {}
    for idx, p in enumerate(pads):
        cx, cy = p["x"], p["y"]
        gx0 = int(math.floor((cx - keep_out_radius) / GRID)) # grid cell boundaries
        gx1 = int(math.ceil((cx + keep_out_radius) / GRID))
        gy0 = int(math.floor((-cy - keep_out_radius) / GRID))
        gy1 = int(math.ceil((-cy + keep_out_radius) / GRID))
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                x, y = gx * GRID, -gy * GRID
                if (x - cx) ** 2 + (y - cy) ** 2 <= keep_out_radius * keep_out_radius:
                    cover.setdefault((gx, gy), set()).add(idx)
    return cover

def route_wire(start_cell, goal_cell, own_pads, cover, occ, nxny, net_id, num_layers):
    nx, ny = nxny
    gx_goal, gy_goal = goal_cell

    def passable(gx, gy, layer):
        if gx < 0 or gy < 0 or gx > nx or gy > ny:
            return False
        c = cover.get((gx, gy))
        if c and not c.issubset(own_pads):     # a foreign pad covers this cell
            return False
        o = occ.get((gx, gy, layer))
        return o is None or o == net_id

    start = (start_cell[0], start_cell[1], 0)
    if not passable(*start):
        return None

    def h(gx, gy):
        return abs(gx - gx_goal) + abs(gy - gy_goal)

    openh = [(h(*start_cell), 0.0, start)]
    gscore = {start: 0.0}
    came = {}
    while openh:
        f, g, s = heapq.heappop(openh)
        if g > gscore.get(s, 1e18):
            continue
        gx, gy, layer = s
        if gx == gx_goal and gy == gy_goal and layer == 0:
            path = [s]
            while s in came:
                s = came[s]
                path.append(s)
            return path[::-1]
        neigh = [(gx + 1, gy, layer, 1.0), (gx - 1, gy, layer, 1.0),
                 (gx, gy + 1, layer, 1.0), (gx, gy - 1, layer, 1.0)]
        if layer + 1 < num_layers:                       # via up to next metal
            neigh.append((gx, gy, layer + 1, VIA_COST))
        if layer - 1 >= 0:                               # via down to prev metal
            neigh.append((gx, gy, layer - 1, VIA_COST))
        for ngx, ngy, nl, cost in neigh:
            if not passable(ngx, ngy, nl):
                continue
            ns = (ngx, ngy, nl)
            step = 0.0 if occ.get(ns) == net_id else cost   # reuse own net free
            ng = g + step
            if ng < gscore.get(ns, 1e18):
                gscore[ns] = ng
                came[ns] = s
                heapq.heappush(openh, (ng + h(ngx, ngy), ng, ns))
    return None

def route_nets(nets, pads, nxny, cover, num_layers): # connects all the input and output pads in a net
    occ = {}
    pad_idx = {id(p): i for i, p in enumerate(pads)}
    net_paths = [[] for _ in nets]
    fails = []
    # Route big fan-outs first (while the grid is emptiest).
    order = sorted(range(len(nets)), key=lambda k: -len(nets[k]["outputs"]))
    for k in order:
        net = nets[k]
        own = {pad_idx[id(net["input"])]} | {pad_idx[id(o)] for o in net["outputs"]}
        start = wire_router_grid(net["input"])
        for o in net["outputs"]:
            path = route_wire(start, wire_router_grid(o), own, cover, occ, nxny, k, num_layers)
            if path is None:
                fails.append((net["input"]["name"], o["name"]))
                continue
            for st in path:
                occ[st] = k
            net_paths[k].append(path)
    return net_paths, fails

def path_to_segs(path): #converts a raw grid path into straight wire segments and via locations
    runs, vias, cur = [], [], [path[0]]
    for j in range(1, len(path)):
        a, b = path[j - 1], path[j]
        if a[2] != b[2]:                      # layer change -> via
            runs.append(cur)
            vias.append((a[0], a[1], min(a[2], b[2])))   # via between metal k and k+1
            cur = [b]
        else:
            cur.append(b)
    runs.append(cur)

    segs = []
    for run in runs:
        if len(run) < 2:
            continue
        layer = run[0][2]
        pts = [(c[0], c[1]) for c in run]
        start, pdir = pts[0], None
        for i in range(1, len(pts)):
            d = (pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
            if pdir is None:
                pdir = d
            elif d != pdir:
                segs.append((start, pts[i - 1], layer))
                start, pdir = pts[i - 1], d
        segs.append((start, pts[-1], layer))
    return segs, vias

def pad_poly(p): # creates the GDS structure for a pad
    h = PAD_SIZE / 2.0
    return gdstk.rectangle((p["x"] - h, p["y"] - h), (p["x"] + h, p["y"] + h),
                           layer=metal_layer(0), datatype=METAL_DT)

def via_polys(pt, k): # creates GDS structure for vias
    h = VIA_SIZE / 2.0
    x, y = pt
    return [gdstk.rectangle((x - h, y - h), (x + h, y + h), layer=L, datatype=d)
            for L, d in ((via_layer(k), VIA_DT),
                         (metal_layer(k), METAL_DT),
                         (metal_layer(k + 1), METAL_DT))]

def build(pads, nets, net_paths): #draws the layout
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell("IO_WIRED")
    if ADD_DIE_OUTLINE:
        cell.add(gdstk.rectangle((0.0, 0.0), (DIE_W, -DIE_H),
                                 layer=BOUNDARY_LAYER, datatype=BOUNDARY_DT))
    geo = {}

    def add(key, poly):
        geo.setdefault(key, []).append(poly)
        cell.add(poly)

    pad_key = {}
    for k, net in enumerate(nets):
        pad_key[id(net["input"])] = ("net", k)
        for o in net["outputs"]:
            pad_key[id(o)] = ("net", k)

    for i, p in enumerate(pads):
        key = pad_key.get(id(p), ("pad", i))
        add(key, pad_poly(p))
        if ADD_LABELS and p["name"]:
            cell.add(*gdstk.text(p["name"], LABEL_SIZE,
                                 (p["x"] - PAD_SIZE / 2, p["y"] + PAD_SIZE / 2),
                                 layer=LABEL_LAYER, datatype=LABEL_DT))
        # Tag input/output pads with their role, just above the name.
        if ADD_LABELS and p["io"]:
            cell.add(*gdstk.text(p["io"].capitalize(), LABEL_SIZE,
                                 (p["x"] - PAD_SIZE / 2,
                                  p["y"] + PAD_SIZE / 2 + LABEL_SIZE * 1.2),
                                 layer=LABEL_LAYER, datatype=IO_LABEL_DT))

    for k, paths in enumerate(net_paths):
        for path in paths:
            segs, vias = path_to_segs(path)
            for a, b, layer in segs:
                for poly in gdstk.FlexPath([grid_to_coordinates(a), grid_to_coordinates(b)],
                                           WIRE_WIDTH, layer=metal_layer(layer),
                                           datatype=METAL_DT).to_polygons():
                    add(("net", k), poly)
            for gx, gy, via_k in vias:
                for poly in via_polys(grid_to_coordinates((gx, gy)), via_k):
                    add(("net", k), poly)
    return lib, geo

def group_bbox(polys): # quick check for shorts
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

def find_shorts(geo, num_layers): # checks for shorts
    layers = tuple(metal_layer(k) for k in range(num_layers)) + \
        tuple(via_layer(k) for k in range(num_layers - 1))
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
                continue                      # bounding boxes disjoint
            for L in layers:
                pa, pb = by_layer[(ids[a], L)], by_layer[(ids[b], L)]
                if pa and pb and gdstk.boolean(pa, pb, "and"):
                    shorts.append((ids[a], ids[b], L))
    return shorts

def pair_one_to_one(grp_inputs, outputs):#split across several chips
    """Pair each input in the group to a distinct nearest output (1:1)."""
    free, nets = list(outputs), []
    for inp in grp_inputs:
        j = min(range(len(free)), key=lambda k: dist(inp, free[k]))
        nets.append({"input": inp, "outputs": [free.pop(j)]})
    return nets

def make_chips(inputs, outputs):
    n = len(outputs)
    return [pair_one_to_one(inputs[s:s + n], outputs)
            for s in range(0, len(inputs), n)]

def build_chip(pads, nets, nxny, cover):
    net_paths, fails, used = [], [], MIN_LAYERS
    for n in range(MIN_LAYERS, MAX_LAYERS + 1):
        net_paths, fails = route_nets(nets, pads, nxny, cover, n)
        used = n
        if not fails:
            break
    lib, geo = build(pads, nets, net_paths)
    shorts = find_shorts(geo, used)
    return lib, net_paths, fails, used, shorts

def write_pairs(path, nets):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["input_pad", "input_signal", "output_pad", "output_signal",
                    "outputs_on_this_input"])
        for net in nets:
            n = len(net["outputs"])
            for o in net["outputs"]:
                w.writerow([net["input"]["name"], net["input"]["signal"],
                            o["name"], o["signal"], n])

def main():
    csv_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else INPUT_CSV
    pads = read_pads(csv_path)
    inputs = [p for p in pads if p["io"] == "input"]
    outputs = [p for p in pads if p["io"] == "output"]

    print(f"{len(inputs)} inputs, {len(outputs)} outputs, "
          f"{len(pads) - len(inputs) - len(outputs)} others.")
    if not inputs or not outputs:
        raise SystemExit("Need at least one input and one output.")

    nxny = (int(DIE_W / GRID) + 2, int(DIE_H / GRID) + 2)
    cover = build_cover(pads)
    OUTPUT_DIR.mkdir(exist_ok=True)

    if len(inputs) > len(outputs):
        # More inputs than outputs: one chip per len(outputs) inputs, each
        # wiring its inputs 1:1 to the outputs (the last chip may be partial).
        chips = make_chips(inputs, outputs)
        print(f"More inputs than outputs -> {len(chips)} chip(s); each wires "
              f"up to {len(outputs)} input(s) 1:1 to the outputs.")
    else:
        nets = assign_outputs(inputs, outputs)
        counts = sorted(len(n["outputs"]) for n in nets)
        hist = {c: counts.count(c) for c in sorted(set(counts))}
        print(f"Fan-out per input (outputs:#inputs): {hist}  "
              f"(min {counts[0]}, max {counts[-1]})")
        chips = [nets]

    multi = len(chips) > 1
    width = max(2, len(str(len(chips))))
    for ci, nets in enumerate(chips, 1):
        lib, net_paths, fails, used, shorts = build_chip(pads, nets, nxny, cover)
        n_conn = sum(len(p) for p in net_paths)
        tag = f"chip {ci}/{len(chips)}" if multi else "single chip"
        print(f"[{tag}] {len(nets)} net(s), {used} layer(s), "
              f"{n_conn} routed, {len(fails)} failed.")
        for a, b in fails:
            print(f"    FAILED: {a} -> {b}")
        if shorts:
            print(f"    VERIFY FAILED: {len(shorts)} overlap(s):")
            for a, b, L in shorts[:6]:
                print(f"      {a} <-> {b} on layer {L}")
            raise SystemExit("Refusing to write a layout with overlaps.")

        stem = f"chip{ci:0{width}d}" if multi else (csv_path.stem + "_v2")
        gds_out = OUTPUT_DIR / f"{stem}_io_wired.gds"
        pairs_out = OUTPUT_DIR / f"{stem}_io_pairs.csv"
        lib.write_gds(gds_out)
        write_pairs(pairs_out, nets)
        print(f"    wrote {gds_out.name}")

if __name__ == "__main__":
    main()
