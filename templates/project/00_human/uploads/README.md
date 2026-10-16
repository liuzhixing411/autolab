# Your uploads

One subfolder per item, e.g. `uploads/2026-10-20_PIV_bubble_jet/`:
1. copy `_TEMPLATE/meta.toml` into it and fill it in;
2. put the data files in the same folder (any format).

Each turn the system scans this folder: items with valid metadata get a U-number and are searched whenever a new direction starts (the "must read" context pack); invalid ones are reported in ACTION_NEEDED with what is missing.
Items with `use_as = "constraint"` (for example experimental data a result must match) appear in every context pack.
