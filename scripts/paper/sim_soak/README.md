# Simulation results for the paper (HIL soaks)

Scripted, repeatable tables and figures for the paper's simulation
subsection: no wind (HIL soak run 39) and wind (Gazebo wind, runs 42 on).
The agreed plan is `runtime/soak/20260929/PRESENTATION_PLAN.md`.

Build from the workspace root on the host:

```bash
scripts/paper/sim_soak/build.sh
```

It reads the bags inside the devcontainer (`extract_all.py`, needs ROS) into
the cache, then renders on the host (`make_all.py`), where Nimbus Sans, the
manuscript's Helvetica, is installed. `paper_style.py` holds the
manuscript's palette (from its draw.io figures) and table style (LaTeX
`table*` like the manuscript's Table 1; needs `xcolor` with `table` and
`array`).

Outputs go to `runtime/paper/sim_soak/` (PDF and PNG figures; tables as CSV,
Markdown and LaTeX; `summary.json`; `MISSING.md` lists what could not be
built yet). Bag extraction is cached per bag folder and size, so a re-run
reads only new or grown bags and takes seconds.

| File | Content |
| --- | --- |
| `runs.yaml` | Run catalogue: per run the run directory, Pi node logs, bag roots, soak TSVs, the end of its mission window, how it ended; the runs used by the single-run figures; infrastructure interruptions. A running run has `end: null` (= now). |
| `soak_data.py` | Node-log parsing: landing phases and their outcome, abort triggers, behaviour-tree branch activations, mission mode intervals, cycles. No ROS needed. |
| `bag_extract.py` | Bag signals: battery at 1 Hz and PX4 odometry inter-arrival statistics per bag folder; Figure 2's window signals. |
| `make_all.py` | Tables 1-2 and Figures 1-6. |

Definitions:

- **Landing phase:** a Reach Cable start to the next Cable Charging or
  Inspection start, or to Reach Cable failing. Captured = a cable landing
  succeeded, or the gripper-closed check after a reported landing failure
  found the gripper closed (late capture). Limit reached = Reach Cable failed
  after 3 aborted landings (a validation setting). Interrupted = Reach Cable
  failed otherwise (infrastructure). Phases still open at a run's end are left
  out.
- **Abort trigger:** the V-gate or above-target violation logged within 2 s
  before the abort.
- **Branch activations:** log signatures in `soak_data.SIG`.
- **Applied wind (Figure 2):** `/simulation/wind/velocity` when the bag has it
  (run 47 on), otherwise reconstructed from `/clock` with the WindEffects gust
  model (deterministic, no noise; parameters from the world SDF).
- **Tracking deviation (Figure 2):** controller reference (III world frame:
  north, west, up) against PX4 odometry (north, east, down). The reference
  carries no position during the cable landing maneuver itself, so the trace
  has gaps there.
- **Odometry inter-arrival (Figure 6):** receipt times at the Pi's recorder;
  gaps over 30 s (recording pauses between bag folders) are left out.

To switch the wind figures to a later run (e.g. run 47 once it is long), set
`figure_runs.wind` in `runs.yaml`; to close a finished run, set its `end`.
