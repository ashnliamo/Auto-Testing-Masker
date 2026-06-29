"""
Realistic tests for the routing algorithm and resistor generation.

Builds synthetic-but-realistic peripheral pad rings (various group sizes, spread
around the ring, with cramped clusters and corner pads), runs the REAL generator
pipeline on each (io_pair_wiring_parallel.size_and_place / build_chip / draw_group)
and checks:

  * cross-net shorts        - find_shorts() must report 0 (no two NETS overlap).
  * return-vs-comb crossings - each input's return wire must not cross any OTHER
                              input's comb (a within-group short find_shorts can't
                              see, since they share the group net).
  * resistance accuracy     - every coil's built resistance matches its binary
                              target to < 0.5 %.
  * parallel decodability   - the group's conductances are sum-distinct (so a
                              parallel reading maps to a unique missing set), and
                              we report the decode margin (smallest gap between any
                              two subset sums, relative to the all-on conductance).

Also prints feasibility metrics (die size, deepest comb, largest resistor) so the
binary-ladder cost is visible.

Run:  py tests/routing_tests.py
"""

import sys
import pathlib

import gdstk

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import io_pair_wiring_parallel as iopw     # noqa: E402

PASS, FAIL = "PASS", "FAIL"


# ----------------------------------------------------------------------
# Synthetic pad rings
# ----------------------------------------------------------------------
def ring_pads(cols, rows, pitch=170.0):
    """A rectangular ring: `cols` pads along the top and bottom, `rows` along the
    left and right (corners shared). Returns pad dicts, all inert to start."""
    W, H = (cols + 1) * pitch, (rows + 1) * pitch
    pts = []
    for i in range(cols + 2):                       # bottom & top (incl corners)
        pts.append((i * pitch, 0.0))
        pts.append((i * pitch, H))
    for j in range(1, rows + 1):                    # left & right (no corners)
        pts.append((0.0, j * pitch))
        pts.append((W, j * pitch))
    pads = []
    for n, (x, y) in enumerate(pts):
        pads.append({"name": f"P{n}", "signal": f"s{n}", "x": x, "y": y,
                     "io": "", "group": None})
    return pads


def _perimeter_order(pads):
    import math
    cx = sum(p["x"] for p in pads) / len(pads)
    cy = sum(p["y"] for p in pads) / len(pads)
    return sorted(range(len(pads)),
                  key=lambda i: math.atan2(pads[i]["y"] - cy, pads[i]["x"] - cx))


def assign(pads, specs, step=4):
    """Tag pads into groups. `specs` = list of (group_num, n_inputs, n_outputs).
    `step` controls placement around the ring: step>1 SPREADS each group's pads
    around the perimeter; step==1 CLUSTERS them into a tight arc (stresses the
    cramped-routing path)."""
    order = _perimeter_order(pads)
    seq = []                                        # a permutation of `order`
    for off in range(step):
        seq.extend(order[off::step])
    need = sum(n_in + n_out for _, n_in, n_out in specs)
    assert need <= len(seq), f"need {need} pads but ring has {len(seq)}"
    it = iter(seq)
    for num, n_in, n_out in specs:
        for _ in range(n_out):
            pads[next(it)].update(io="output", group=num)
        for _ in range(n_in):
            pads[next(it)].update(io="input", group=num)
    return pads


# ----------------------------------------------------------------------
# Checks on a placed design
# ----------------------------------------------------------------------
def subset_sum_margin(conductances):
    """Smallest gap between any two distinct subset sums of `conductances`, relative
    to the all-on sum. > 0 => parallel reading is uniquely decodable; the value is
    the fractional measurement resolution the decode needs."""
    n = len(conductances)
    sums = sorted(sum(conductances[i] for i in range(n) if mask >> i & 1)
                  for mask in range(1 << n))
    total = sums[-1] if sums[-1] else 1.0
    gap = min((sums[i + 1] - sums[i] for i in range(len(sums) - 1)), default=total)
    return gap / total


def to_coupons(groups):
    return iopw.split_coupons(groups, iopw.MAX_BINARY_INPUTS)


def evaluate(pads):
    inputs = [p for p in pads if p["io"] == "input"]
    outputs = [p for p in pads if p["io"] == "output"]
    groups = iopw.assign_groups(inputs, outputs)
    coupons = to_coupons(groups)
    iopw.size_and_place(pads, coupons)
    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    bounds = (min(xs), max(xs), min(ys), max(ys))
    plane = iopw.central_plane(bounds)
    edge_coords = iopw.near_edge_coords(pads, bounds)

    # cross-net shorts, real chip packing (two coupons per chip, both metal layers)
    shorts = 0
    for ci, cc in enumerate(iopw.pack_chips(coupons, 2), 1):
        _, geo, _ = iopw.build_chip(ci, cc, pads, bounds)
        shorts += len(iopw.find_shorts(geo, len(cc)))

    crossings, max_rerr, worst_margin, max_R, max_prot = 0, 0.0, 1.0, 0.0, 0.0
    for g in coupons:
        combs, rets, Rs = {}, {}, []
        for k, inp in enumerate(iopw.ordered_inputs(g), 1):
            e = iopw.edge_of(inp, bounds)
            u, v = iopw.inward_along(e)
            cu = (-u[0], -u[1])
            a = inp["x"] * v[0] + inp["y"] * v[1]
            s, span = iopw.coil_build(inp, g, bounds, edge_coords)
            coil, ret, clen = iopw.build_coil(inp["x"], inp["y"], cu, v, a, s, span,
                                              iopw.input_target_len(k), edge_coords[e],
                                              plane)
            R = iopw.SHEET_RES * clen / iopw.WIRE_WIDTH
            Rs.append(R)
            max_R = max(max_R, R)
            max_rerr = max(max_rerr, abs(R - iopw.input_target_r(k))
                           / iopw.input_target_r(k))
            cx = [p[0] for p in coil]
            cy = [p[1] for p in coil]
            max_prot = max(max_prot, bounds[0] - min(cx), max(cx) - bounds[1],
                           bounds[2] - min(cy), max(cy) - bounds[3])
            combs[k] = gdstk.FlexPath(coil, iopw.WIRE_WIDTH).to_polygons()
            rets[k] = gdstk.FlexPath(ret, iopw.WIRE_WIDTH).to_polygons()
        for ki, rt in rets.items():
            for kj, cb in combs.items():
                if ki != kj and gdstk.boolean(rt, cb, "and"):
                    crossings += 1
        worst_margin = min(worst_margin, subset_sum_margin([1.0 / R for R in Rs]))

    return {"groups": len(coupons), "inputs": len(inputs), "shorts": shorts,
            "crossings": crossings, "max_rerr": max_rerr, "margin": worst_margin,
            "die": (iopw.DIE_W, iopw.DIE_H), "max_R": max_R, "max_prot": max_prot}


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------
def scenarios():
    yield "two_small_groups", assign(ring_pads(8, 5), [(0, 4, 1), (1, 4, 2)])
    yield "one_group_8in", assign(ring_pads(10, 6), [(0, 8, 2)])
    yield "many_tiny_groups", assign(ring_pads(10, 6),
                                     [(0, 2, 1), (1, 3, 1), (2, 2, 1), (3, 4, 1)])
    yield "cramped_cluster", assign(ring_pads(12, 7),
                                    [(0, 6, 1), (1, 6, 1)], step=1)
    yield "corner_heavy", assign(ring_pads(6, 4), [(0, 5, 1), (1, 4, 1)], step=1)
    yield "single_pair", assign(ring_pads(6, 4), [(0, 2, 1)])
    yield "split_14in", assign(ring_pads(10, 6), [(0, 14, 1)])   # 14 -> 8 + 6 coupons


def main():
    print(f"LADDER = binary, base = {iopw.COIL_BASE_R:.0f} ohm\n")
    header = (f"{'scenario':<18}{'coup':>5}{'in':>4}{'shorts':>7}{'cross':>6}"
              f"{'R_err%':>8}{'decode%':>9}{'maxR':>10}{'prot_um':>9}"
              f"{'die_mm':>12}  result")
    print(header)
    print("-" * len(header))
    all_ok = True
    for name, pads in scenarios():
        try:
            r = evaluate(pads)
        except SystemExit as e:
            print(f"{name:<18}  EXCEPTION: {e}")
            all_ok = False
            continue
        ok = (r["shorts"] == 0 and r["crossings"] == 0 and r["max_rerr"] < 0.005
              and r["margin"] > 0)
        all_ok &= ok
        dw, dh = r["die"]
        print(f"{name:<18}{r['groups']:>5}{r['inputs']:>4}{r['shorts']:>7}"
              f"{r['crossings']:>6}{r['max_rerr']*100:>8.3f}{r['margin']*100:>9.3f}"
              f"{r['max_R']:>10.0f}{r['max_prot']:>9.0f}"
              f"{dw/1000:>5.1f}x{dh/1000:<5.1f}  {PASS if ok else FAIL}")
    print()
    if all_ok:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
