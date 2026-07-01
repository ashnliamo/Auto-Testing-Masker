import csv
import sys
import pathlib

HERE = pathlib.Path(__file__).parent
DECODE_DIR = HERE / "decode_inputs"

CHIP, LAYER, PAD, R_COL = "chip", "layer", "input_pad", "actual_R_ohm"
MATCH_TOL = 0.05   # accept a subset if its predicted R is within this of measured


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------
def find_decode_csv():
    """The single CSV in decode_inputs/ (any name). Errors if none or several."""
    csvs = sorted(DECODE_DIR.glob("*.csv"))
    if not csvs:
        raise SystemExit(f"No .csv found in {DECODE_DIR} -- put the generator's "
                         f"*_parallel.csv there.")
    if len(csvs) > 1:
        names = ", ".join(p.name for p in csvs)
        raise SystemExit(f"Multiple .csv files in {DECODE_DIR} ({names}); keep just one, "
                         f"or pass the path: py find_missing_probes.py <file.csv>")
    return csvs[0]


def load(path):
    if not path.exists():
        raise SystemExit(f"Input CSV not found: {path}\n"
                         f"Put pinout_grouped_parallel.csv in {path.parent}\\")
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"No rows in {path}")
    for col in (CHIP, LAYER, PAD, R_COL):
        if col not in rows[0]:
            raise SystemExit(f"{path} has no '{col}' column.")
    return rows


def ask_choice(prompt, choices):
    choices = sorted(choices)
    while True:
        s = input(prompt).strip()
        try:
            v = int(s)
        except ValueError:
            print(f"  Enter a whole number from {choices}.")
            continue
        if v in choices:
            return v
        print(f"  Available: {choices}")


def parse_ohms(s):
    """'1.2k'/'470'/'3.3M'/'OPEN' -> ohms (inf for open), or None if unparseable."""
    s = s.strip().lower().replace(",", "").replace("ohm", "").replace("Ω", "").strip()
    if s in ("", "open", "ol", "inf", "overrange", "over"):
        return float("inf")
    mult = 1.0
    if s.endswith("meg"):
        mult, s = 1e6, s[:-3]
    elif s.endswith("k"):
        mult, s = 1e3, s[:-1]
    elif s.endswith("m"):
        mult, s = 1e6, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Calibration offsets (real resistance != theoretical)
# ----------------------------------------------------------------------
def canonical_rungs(rows, tol=0.05):
    """Distinct resistance rungs in the CSV. The binary ladder doubles per rung, so
    values within `tol` are one rung; returns each cluster's lowest value, ascending."""
    rungs = []
    for v in sorted(float(r[R_COL]) for r in rows):
        if not rungs or v > rungs[-1] * (1 + tol):
            rungs.append(v)
    return rungs


def ask_calibration(rungs):
    """Optionally read each ladder rung off the calibration chip, returning
    {rung: offset} with offset = measured - theoretical (reused for every chip/layer).
    Blank leaves a rung uncorrected; declining at the first prompt skips all."""
    if input("\nApply calibration offsets (measure the calibration chip)? [y/N]: "
             ).strip().lower() not in ("y", "yes"):
        return {}
    print("For each theoretical resistance, enter the MEASURED value of the matching\n"
          "calibration resistor (blank to leave that rung uncorrected):")
    offsets = {}
    for rung in rungs:
        m = parse_ohms(input(f"   ~{rung:,.0f} ohm calibration resistor measures: "))
        if m is None:
            print("     couldn't read that -- left uncorrected.")
        elif m != float("inf"):
            offsets[rung] = m - rung
            print(f"     offset {m - rung:+,.2f} ohm")
    return offsets


def apply_offsets(resistors, rungs, offsets):
    """Add each resistor's nearest-rung offset so the values match real hardware.
    Returns a new (pad, R) list sorted by corrected resistance."""
    def corrected(R):
        return R + offsets.get(min(rungs, key=lambda v: abs(v - R)), 0.0)
    return sorted(((pad, corrected(R)) for pad, R in resistors), key=lambda t: t[1])


# ----------------------------------------------------------------------
# Decode
# ----------------------------------------------------------------------
def parallel(resistances):
    g = sum(1.0 / r for r in resistances if r > 0)
    return 1.0 / g if g > 0 else float("inf")


def decode_margin(conductances):
    """Smallest gap between any two subset sums, relative to the all-on sum: the
    reading must beat this fraction for the missing set to be unique."""
    n = len(conductances)
    sums = sorted(sum(conductances[i] for i in range(n) if mask >> i & 1)
                  for mask in range(1 << n))
    total = sums[-1] or 1.0
    return min((sums[i + 1] - sums[i] for i in range(len(sums) - 1)),
               default=total) / total


def rank_subsets(resistors, r_meas):
    """Score every subset by how well its REMOVAL explains r_meas (the rest reads
    1/(G_all - Gsub)). Returns (pred_R, missing_idx) list, best fit first."""
    g = [1.0 / R for _, R in resistors]
    G_all = sum(g)
    G_meas = 1.0 / r_meas if r_meas not in (0, float("inf")) else (
        0.0 if r_meas == float("inf") else G_all)
    G_missing = G_all - G_meas                       # conductance that dropped out
    n = len(resistors)
    scored = []
    for mask in range(1 << n):
        gsub = sum(g[i] for i in range(n) if mask >> i & 1)
        g_left = G_all - gsub
        pred = 1.0 / g_left if g_left > 1e-12 else float("inf")
        idx = tuple(i for i in range(n) if mask >> i & 1)
        scored.append((abs(gsub - G_missing), pred, idx))
    scored.sort(key=lambda t: t[0])
    return [(pred, idx) for _, pred, idx in scored]


def report(resistors, r_meas):
    pads = [p for p, _ in resistors]
    r_all = parallel([R for _, R in resistors])
    margin = decode_margin([1.0 / R for _, R in resistors])
    print(f"\nAll-contacting parallel resistance: {r_all:,.2f} ohm "
          f"({len(resistors)} resistor(s)).")
    margin_ohm = margin * r_all                      # resolution near the all-on reading
    print(f"Decode margin {margin*100:.2f}% (~{margin_ohm:,.2f} ohm): the reading must "
          f"be accurate to better than this (incl. contact resistance) for the missing "
          f"set to be unique.")

    if r_meas != float("inf") and r_meas < r_all * (1 - MATCH_TOL):
        print(f"  ! Measured {r_meas:,.2f} ohm is BELOW the all-contacting value -- "
              "with every probe landed the reading can't go lower. Check the probe "
              "setup (a short, or the wrong chip/layer).")

    ranked = rank_subsets(resistors, r_meas)
    pred, idx = ranked[0]
    missing = [pads[i] for i in idx]
    present = [pads[i] for i in range(len(pads)) if i not in idx]
    err = abs(pred - r_meas) / r_meas if r_meas not in (0, float("inf")) else (
        0.0 if pred == float("inf") else 1.0)

    print(f"\nMeasured: {('OPEN' if r_meas==float('inf') else f'{r_meas:,.2f} ohm')}"
          f"  ->  best fit predicts {('OPEN' if pred==float('inf') else f'{pred:,.2f} ohm')}"
          f"  ({err*100:.1f}% off)")
    if not missing:
        print("  => ALL probes in contact (no resistor missing).")
    else:
        print(f"  => {len(missing)} probe(s) NOT in contact (missing): "
              f"{', '.join(missing)}")
    print(f"     in contact: {', '.join(present) if present else '(none)'}")

    if err > MATCH_TOL:
        print(f"  ! Best fit is {err*100:.1f}% off (> {MATCH_TOL*100:.0f}%); treat the "
              "result as approximate -- the reading may sit between resistor sums.")
    # flag ambiguity: a different missing set predicting nearly the same resistance
    for pred2, idx2 in ranked[1:]:
        if set(idx2) == set(idx):
            continue
        e2 = abs(pred2 - r_meas) / r_meas if r_meas not in (0, float("inf")) else (
            0.0 if pred2 == float("inf") else 1.0)
        if e2 <= MATCH_TOL:
            alt = [pads[i] for i in idx2] or ["(none)"]
            print(f"  ! Ambiguous: missing {{{', '.join(alt)}}} fits about as well "
                  f"({e2*100:.1f}% off).")
        break


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else find_decode_csv()
    rows = load(path)
    print(f"Loaded {len(rows)} coil(s) from {path}")
    combos = sorted({(int(r[CHIP]), int(r[LAYER])) for r in rows})
    print(f"Chips available: {sorted({c for c, _ in combos})}")

    rungs = canonical_rungs(rows)
    offsets = ask_calibration(rungs)

    while True:                             # one decode per chip/layer; blank quits
        raw = input("\nChip # (blank to quit): ").strip()
        if raw == "":
            break
        try:
            chip = int(raw)
        except ValueError:
            print("  Enter a whole number."); continue
        chip_layers = sorted({l for c, l in combos if c == chip})
        if not chip_layers:
            print(f"  No chip {chip}. Available: {sorted({c for c, _ in combos})}")
            continue
        layer = ask_choice(f"Layer # {chip_layers}: ", chip_layers)

        sel = [r for r in rows if int(r[CHIP]) == chip and int(r[LAYER]) == layer]
        resistors = sorted(((r[PAD], float(r[R_COL])) for r in sel),
                           key=lambda t: t[1])
        if offsets:
            resistors = apply_offsets(resistors, rungs, offsets)
        note = " (calibration-corrected)" if offsets else ""
        print(f"\nChip {chip}, layer {layer}: {len(resistors)} input resistor(s){note}")
        for pad, R in resistors:
            print(f"   {pad:>12}  {R:10,.2f} ohm")

        ohms = None
        while ohms is None:
            ohms = parse_ohms(input("\nMeasured resistance (e.g. 470, 1.2k, OPEN): "))
            if ohms is None:
                print("  Couldn't read that. Try e.g. 470, 1.2k, 3.3M, or OPEN.")
        report(resistors, ohms)


if __name__ == "__main__":
    main()
