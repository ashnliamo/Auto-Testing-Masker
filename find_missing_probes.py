import csv
import sys
import pathlib

HERE = pathlib.Path(__file__).parent
DEFAULT_CSV = HERE / "decode_inputs" / "pinout_grouped_parallel.csv"

CHIP, LAYER, PAD, R_COL = "chip", "layer", "input_pad", "actual_R_ohm"
# A subset is accepted as a clean match when the resistance it predicts is within
# this fraction of the measured value (covers contact resistance + meter error).
MATCH_TOL = 0.05


# ----------------------------------------------------------------------
# Input
# ----------------------------------------------------------------------
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
    """Parse a resistance like '1.2k', '470', '3.3M', 'OPEN'. Returns ohms (inf for
    an open / over-range reading), or None if it can't be parsed."""
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
# Decode
# ----------------------------------------------------------------------
def parallel(resistances):
    g = sum(1.0 / r for r in resistances if r > 0)
    return 1.0 / g if g > 0 else float("inf")


def decode_margin(conductances):
    """Smallest gap between any two distinct subset sums of the conductances,
    relative to the all-on sum. A parallel reading resolves the missing set only if
    it is accurate to better than ~this fraction of the all-contacting value."""
    n = len(conductances)
    sums = sorted(sum(conductances[i] for i in range(n) if mask >> i & 1)
                  for mask in range(1 << n))
    total = sums[-1] or 1.0
    return min((sums[i + 1] - sums[i] for i in range(len(sums) - 1)),
               default=total) / total


def rank_subsets(resistors, r_meas):
    """Score every subset of `resistors` by how well its REMOVAL explains r_meas.
    A removed subset has conductance Gsub; the network that remains reads
    1/(G_all - Gsub). Returns the subsets as (pred_R, missing_idx_tuple), best fit
    first. `resistors` is a list of (pad, R)."""
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
    print(f"Decode margin {margin*100:.2f}%: the reading must be accurate to better "
          f"than this (incl. contact resistance) for the missing set to be unique.")

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
    # flag ambiguity: another subset with a clearly different missing set predicts
    # nearly the same resistance
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
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    rows = load(path)
    print(f"Loaded {len(rows)} coil(s) from {path}")
    combos = sorted({(int(r[CHIP]), int(r[LAYER])) for r in rows})
    print(f"Chips available: {sorted({c for c, _ in combos})}")

    # Re-prompts until a blank chip number is entered.
    while True:
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
        print(f"\nChip {chip}, layer {layer}: {len(resistors)} input resistor(s)")
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
