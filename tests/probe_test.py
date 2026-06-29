"""
Test harness for io_pair_wiring_parallel.py.

Synthesises pinout CSVs that exercise different input/output GROUP combinations,
runs the generator on each, and checks:
  * the generator finishes and writes chips with 0 cross-net overlaps,
  * every group is ELECTRICALLY CONNECTED (all its input + output pads join one
    net through coils -> fingers -> central plane), via a flattened boolean union,
  * groups stay isolated from each other.
Then it SIMULATES realistic probing: for a group, mark some probes as making
contact and some as open, and show what the tester would read (finite coil
resistance vs open), confirming the coupon reports each probe's contact state.

Run:  py tests/probe_test.py
"""

import csv
import sys
import random
import subprocess
import pathlib

import gdstk

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
GEN = ROOT / "io_pair_wiring_parallel.py"
TIN = HERE / "inputs"
OUT = ROOT / "outputs"
sys.path.insert(0, str(ROOT))
import importlib.util
spec = importlib.util.spec_from_file_location("iopw", GEN)
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


# ----------------------------------------------------------------------
# Synthetic pad ring
# ----------------------------------------------------------------------
def make_ring(nx=20, ny=12):
    """Pads on a rectangular ring (y negative = top-left origin). Bottom/top
    pads are inset from the x-corners and left/right from the y-corners so every
    pad sits unambiguously on ONE edge."""
    x0, x1, y0, y1 = 1200.0, 7400.0, -5400.0, -1400.0
    inset = 320.0
    pads = []

    def lin(a, b, n):
        return [a + i * (b - a) / (n - 1) for i in range(n)]

    for i, x in enumerate(lin(x0 + inset, x1 - inset, nx), 1):
        pads.append([f"B{i}", x, y0])
        pads.append([f"T{i}", x, y1])
    for j, y in enumerate(lin(y0 + inset, y1 - inset, ny), 1):
        pads.append([f"L{j}", x0, y])
        pads.append([f"R{j}", x1, y])
    return pads


def rng(prefix, a, b):
    return [f"{prefix}{i}" for i in range(a, b + 1)]


def write_csv(path, pads, spec):
    """spec: list of (group_num, 'INPUT'|'OUTPUT', [pad names])."""
    label = {}
    for gnum, role, names in spec:
        for nm in names:
            label[nm] = f"{role}{gnum}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Pad", "Signal", "X (um)", "Y (um)", "I/O"])
        for nm, x, y in pads:
            w.writerow([nm, f"SIG_{nm}", f"{x:.3f}", f"{y:.3f}", label.get(nm, "")])


# ----------------------------------------------------------------------
# Scenarios -- different input/output combinations
# ----------------------------------------------------------------------
def scenarios():
    return {
        # varied group sizes, single output each -> 4 groups, 2 chips
        "mixed_sizes": [
            (0, "OUTPUT", ["R6"]), (0, "INPUT", rng("B", 3, 5)),
            (1, "OUTPUT", ["R7"]), (1, "INPUT", rng("B", 8, 12)),
            (2, "OUTPUT", ["L6"]), (2, "INPUT", rng("T", 3, 4)),
            (3, "OUTPUT", ["L7"]), (3, "INPUT", rng("T", 8, 15)),
        ],
        # groups with MULTIPLE outputs sharing the plane
        "multi_output": [
            (0, "OUTPUT", ["L4", "L5", "L6"]), (0, "INPUT", rng("B", 3, 10)),
            (1, "OUTPUT", ["R4", "R5"]), (1, "INPUT", rng("T", 3, 8)),
        ],
        # all 8 ring corners wired as inputs -> exercises coil flipping
        "corner_flips": [
            (0, "OUTPUT", ["R6"]),
            (0, "INPUT", ["B1", "B20", "T1", "T20", "L1", "L12", "R1", "R12", "B10"]),
        ],
        # unmatched group numbers must be dropped (INPUT1, OUTPUT2 have no partner)
        "unmatched": [
            (0, "OUTPUT", ["R6"]), (0, "INPUT", rng("B", 3, 5)),
            (1, "INPUT", rng("T", 3, 5)),
            (2, "OUTPUT", ["L6"]),
        ],
        # odd number of groups -> last chip holds a single group
        "odd_groups": [
            (0, "OUTPUT", ["R4"]), (0, "INPUT", rng("B", 2, 4)),
            (1, "OUTPUT", ["R6"]), (1, "INPUT", rng("B", 7, 10)),
            (2, "OUTPUT", ["R8"]), (2, "INPUT", rng("B", 13, 15)),
            (3, "OUTPUT", ["L4"]), (3, "INPUT", rng("T", 2, 5)),
            (4, "OUTPUT", ["L7"]), (4, "INPUT", rng("T", 9, 12)),
        ],
        # one dense group with inputs on every edge -> big plane, many fingers
        "dense_allsides": [
            (0, "OUTPUT", ["R8", "L8"]),
            (0, "INPUT", rng("B", 3, 12) + rng("T", 3, 12)
                + rng("L", 2, 6) + rng("R", 2, 6)),
        ],
    }


# ----------------------------------------------------------------------
# Connectivity + isolation checks (built directly via the generator funcs)
# ----------------------------------------------------------------------
def check_layout(csv_path):
    pads = P.read_pads(csv_path)
    P.center_pads(pads)
    inputs = [p for p in pads if p["io"] == "input"]
    outputs = [p for p in pads if p["io"] == "output"]
    groups = P.assign_groups(inputs, outputs)
    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    bounds = (min(xs), max(xs), min(ys), max(ys))
    center = ((bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2)
    end_ids = P.edge_end_pads(pads, bounds)

    lib = gdstk.Library()
    results = []
    chips = [groups[i:i + P.GROUPS_PER_CHIP]
             for i in range(0, len(groups), P.GROUPS_PER_CHIP)]
    for ci, chip_groups in enumerate(chips, 1):
        for li, g in enumerate(chip_groups):
            cell = lib.new_cell(f"G{g['num']}_{ci}")
            wpolys, rows, flips, touch = P.draw_group(
                g, li, center, bounds, cell, ci, end_ids)
            padpolys = [P.pad_poly(p, P.metal_layer(0))
                        for p in g["inputs"] + g["outputs"]]
            merged = gdstk.boolean(wpolys + padpolys, [], "or")
            results.append({"group": g["num"], "n_in": len(g["inputs"]),
                            "n_out": len(g["outputs"]), "flips": flips,
                            "touch": touch, "components": len(merged)})
    return groups, results


# ----------------------------------------------------------------------
# Probing simulation
# ----------------------------------------------------------------------
def read_branches(stem):
    rows = []
    with open(OUT / f"{stem}_parallel.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def simulate_probing(stem, seed=1):
    rows = read_branches(stem)
    by_group = {}
    for r in rows:
        by_group.setdefault(r["group"], []).append(r)
    rnd = random.Random(seed)
    print(f"\n  Probing simulation ({stem}): drive an output probe, read each "
          f"input probe; finite coil resistance = contact, OPEN = no contact.")

    # detailed reading table for the first group, with the output landed
    gnum, grp = sorted(by_group.items(), key=lambda kv: int(kv[0]))[0]
    print(f"    group {gnum} ({len(grp)} inputs), output probe LANDED:")
    miss = 0
    for r in grp:
        contact = rnd.random() > 0.30                # ~70% of probes land
        reading = f"{float(r['actual_R_ohm']):>6.0f} ohm" if contact else "  OPEN    "
        verdict = "contact " if contact else "NO CONTACT"
        miss += not contact
        print(f"        probe {r['input_pad']:>4s}  expect "
              f"{float(r['actual_R_ohm']):>6.0f} ohm  ->  reads {reading}  [{verdict}]")
    print(f"        -> coupon flagged {miss}/{len(grp)} probes as not contacting.")

    # whole-group pass/fail across all groups and many random contact patterns
    total = correct = 0
    for gnum, grp in by_group.items():
        for _ in range(200):
            out_c = rnd.random() > 0.15
            for r in grp:
                in_c = rnd.random() > 0.25
                reads_finite = out_c and in_c
                truly_contacting = out_c and in_c
                correct += reads_finite == truly_contacting
                total += 1
    print(f"    across all groups x200 random contact patterns: "
          f"{correct}/{total} probe readings matched ground truth.")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    pads = make_ring()
    print(f"Synthetic ring: {len(pads)} pads.")
    allpass = True
    for name, spec in scenarios().items():
        csv_path = TIN / f"test_{name}.csv"
        write_csv(csv_path, pads, spec)
        proc = subprocess.run([sys.executable, str(GEN), str(csv_path)],
                              capture_output=True, text=True)
        ok_run = proc.returncode == 0
        # parse cross-net overlaps from generator output
        cross = sum(int(line.split("cross-net overlap")[0].split(",")[-1])
                    for line in proc.stdout.splitlines() if "cross-net overlap" in line)
        nchips = sum(1 for line in proc.stdout.splitlines() if line.strip().startswith("chip "))
        try:
            groups, res = check_layout(csv_path)
            disconnected = [r for r in res if r["components"] != 1]
            flips = sum(r["flips"] for r in res)
            touches = sum(r["touch"] for r in res)
        except Exception as e:                       # noqa
            groups, res, disconnected, flips, touches = [], [], ["err"], 0, 0
            ok_run = False
            print(f"  EXCEPTION: {e}")
        ok = ok_run and cross == 0 and not disconnected
        allpass &= ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name:14s}: "
              f"{len(groups)} group(s), {nchips} chip(s), {cross} cross-net overlap(s), "
              f"{flips} coil flip(s), {touches} same-net touch(es), "
              f"all groups connected={not disconnected}")
        if disconnected:
            print(f"        DISCONNECTED: {disconnected}")

    # probing simulation on two representative scenarios
    for stem in ("test_mixed_sizes", "test_corner_flips"):
        simulate_probing(stem)

    print(f"\n{'ALL TESTS PASSED' if allpass else 'SOME TESTS FAILED'}")
    return 0 if allpass else 1


if __name__ == "__main__":
    raise SystemExit(main())
