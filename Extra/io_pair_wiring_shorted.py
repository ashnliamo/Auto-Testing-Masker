import csv
import sys
import pathlib

import gdstk

HERE = pathlib.Path(__file__).parent
INPUT_CSV = HERE / "inputs" / "pinout.csv"
OUTPUT_DIR = HERE / "outputs"

PAD_SIZE = 80.0
STUB_WIDTH = 10.0          # width of the stub from each pad to the rail
RAIL_INSET = 150.0         # rail sits this far inside the pad-ring bounds

ADD_LABELS = True
LABEL_SIZE = 12.0
ADD_DIE_OUTLINE = True
DIE_W, DIE_H = 8213.0, 5199.0   # origin = top-left, +X right, -Y down (assumed)

METAL_LAYER, METAL_DT = 1, 0            # everything is on one metal layer
LABEL_LAYER, LABEL_DT = 101, 0          # pad-name text
IO_LABEL_DT = 1                          # input/output role text
BOUNDARY_LAYER, BOUNDARY_DT = 100, 0     # die outline (marker, not fabricated)

COL_PAD, COL_SIGNAL, COL_X, COL_Y = ("pad", "signal", "x (um)", "y (um)")
INPUT_SIGNAL_PREFIXES = ("VDD", "VSS")
OUTPUT_SIGNAL_PREFIXES = ("VATB",)
OUTPUT_SIGNAL_SUFFIXES = ("_SNS",)

def classify_io(signal):
    s = signal.strip().upper()
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
        pads.append({"name": name, "signal": signal, "x": x, "y": y,
                     "io": classify_io(signal)})
    return pads

def edge_of(p, minx, maxx, miny, maxy, tol=5.0):
    if abs(p["x"] - minx) <= tol:
        return "left"
    if abs(p["x"] - maxx) <= tol:
        return "right"
    if abs(p["y"] - miny) <= tol:
        return "bottom"
    if abs(p["y"] - maxy) <= tol:
        return "top"
    return "interior"

def build(pads):
    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    rx0, rx1 = minx + RAIL_INSET, maxx - RAIL_INSET
    ry0, ry1 = miny + RAIL_INSET, maxy - RAIL_INSET     # ry0 < ry1

    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell("SHORTED")
    if ADD_DIE_OUTLINE:
        cell.add(gdstk.rectangle((0.0, 0.0), (DIE_W, -DIE_H),
                                 layer=BOUNDARY_LAYER, datatype=BOUNDARY_DT))

    # the common rail (one net)
    cell.add(gdstk.rectangle((rx0, ry0), (rx1, ry1),
                             layer=METAL_LAYER, datatype=METAL_DT))

    half = PAD_SIZE / 2.0
    for p in pads:
        x, y = p["x"], p["y"]
        cell.add(gdstk.rectangle((x - half, y - half), (x + half, y + half),
                                 layer=METAL_LAYER, datatype=METAL_DT))
        # stub from the pad to the nearest rail edge, based on which edge it is on
        edge = edge_of(p, minx, maxx, miny, maxy)
        if edge == "bottom":
            stub = [(x, y), (x, ry0)]
        elif edge == "top":
            stub = [(x, y), (x, ry1)]
        elif edge == "left":
            stub = [(x, y), (rx0, y)]
        elif edge == "right":
            stub = [(x, y), (rx1, y)]
        else:                                   # interior: go up to the rail
            stub = [(x, y), (x, ry0)]
        cell.add(gdstk.FlexPath(stub, STUB_WIDTH,
                                layer=METAL_LAYER, datatype=METAL_DT))

        if ADD_LABELS and p["name"]:
            cell.add(*gdstk.text(p["name"], LABEL_SIZE,
                                 (x - half, y + half),
                                 layer=LABEL_LAYER, datatype=LABEL_DT))
        if ADD_LABELS and p["io"]:
            cell.add(*gdstk.text(p["io"].capitalize(), LABEL_SIZE,
                                 (x - half, y + half + LABEL_SIZE * 1.2),
                                 layer=LABEL_LAYER, datatype=IO_LABEL_DT))
    return lib

def main():
    csv_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else INPUT_CSV
    pads = read_pads(csv_path)
    n_in = sum(1 for p in pads if p["io"] == "input")
    n_out = sum(1 for p in pads if p["io"] == "output")
    print(f"{len(pads)} pads ({n_in} inputs, {n_out} outputs) -> one shorted net.")

    lib = build(pads)
    OUTPUT_DIR.mkdir(exist_ok=True)
    gds_out = OUTPUT_DIR / (csv_path.stem + "_shorted_io_wired.gds")
    probes_out = OUTPUT_DIR / (csv_path.stem + "_shorted_probes.csv")
    lib.write_gds(gds_out)
    with open(probes_out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pad", "signal", "io", "x_um", "y_um"])
        for p in pads:
            w.writerow([p["name"], p["signal"], p["io"],
                        f"{p['x']:.3f}", f"{p['y']:.3f}"])

    print(f"Wrote {gds_out}")
    print(f"Wrote {probes_out}")
    print("Readout: a probe is in contact iff it reads finite to any other probe.")

if __name__ == "__main__":
    main()
