#!/usr/bin/env python3
"""Fill the bag cache for every run in runs.yaml (run inside the devcontainer
with ROS sourced). Re-running reads only new or grown bag folders."""
import sys
import time

import bag_extract
import soak_data


def main() -> int:
    cat = soak_data.load_catalogue()
    for run in cat.runs:
        for bag_dir in run.bag_dirs():
            started = time.monotonic()
            series = bag_extract.run_series(bag_dir, cat.cache)
            print(f"run {run.run} {bag_dir.name}: battery {len(series['battery'])} samples, "
                  f"odometry {int(series['odometry_count'])}, gap quantiles {series['gap_quantiles'].round(4)}, "
                  f"{time.monotonic() - started:.0f} s", flush=True)
    # Figure 2's bag window.
    import make_all
    data = make_all.Data(cat)
    window = make_all.fig2_window(data)
    if window is not None:
        run, _phase, _t_ref, t0, t1 = window
        signals = bag_extract.window_signals(run.bag_dirs(), t0, t1, cat.cache)
        print(f"figure 2 window: run {run.run}, " + ", ".join(f"{k} {len(v)}" for k, v in signals.items()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
