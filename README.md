# Probe-Card Contact-Test Mask Generator

Generates GDS test-chip coupons (via [`gdstk`](https://github.com/heitzmann/gdstk)) to verify
which probes on a probe card make contact. Each INPUT pad is wired to a shared central node
through a resistor "comb" of known resistance; probing the structure and reading the resistance
decodes which probes are open.

## Layout

| Path | Purpose |
|------|---------|
| `io_pair_wiring_parallel.py` | Active generator. Reads `inputs/pinout_grouped.csv`, writes GDS + CSV to `outputs/`. |
| `find_missing_probes.py` | Decode tool. Reads `decode_inputs/pinout_grouped_parallel.csv`, prompts for chip/layer/measured-R, reports open probes. |
| `tests/routing_tests.py` | Synthetic routing/decode test suite. |
| `inputs/`, `decode_inputs/` | Input pinout CSVs. |
| `outputs/` | Generated masks (GDS files are git-ignored). |
| `Extra/` | Older/unused generator variants. |

## Usage

```sh
py io_pair_wiring_parallel.py --layers 2   # generate masks
py find_missing_probes.py                  # decode a measurement
py tests/routing_tests.py                  # run tests
```

## Design summary

- Inputs are grouped via the CSV `I/O` column (`INPUTn`/`OUTPUTn` share group `n`).
- Each group becomes one or more **coupons** (one metal layer each). A binary resistance ladder
  (`COIL_BASE_R * 2^(k-1)`) makes the parallel reading decode uniquely to the missing probe set.
- Groups larger than `MAX_BINARY_INPUTS` are split into independent sub-coupons.
- Combs route as radial teeth outside a central plane; returns drop straight through pad gaps so
  traces never cross another net. Die is sized to clear the farthest comb by `DIE_MARGIN_BUFFER`.
- A separate self-sized **calibration coupon** provides known reference resistors.

Key parameters live at the top of `io_pair_wiring_parallel.py`.
