#!/usr/bin/env python3
"""Build every table and figure of the paper's simulation subsection.

Run inside the devcontainer with ROS sourced (bag reading needs rosbag2_py):
    python3 scripts/paper/sim_soak/make_all.py
Inputs come from runs.yaml; outputs go to its `output` folder:
    table1_campaign_summary.{csv,md,tex}
    table2_branch_coverage.{csv,md,tex}
    fig1_attempts_to_capture.{pdf,png}
    fig2_recovered_landing_trace.{pdf,png}
    fig3_run_timelines.{pdf,png}
    fig4_battery_and_modes.{pdf,png}
    fig5_cycle_time.{pdf,png}
    fig6_computational_load.{pdf,png}
    summary.json, MISSING.md (what could not be built yet, and why)
Bag extraction is cached (bag_extract.py), so re-runs are fast.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import math
import pathlib
import statistics
import sys
import xml.etree.ElementTree as ET

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import bag_extract as bx  # noqa: E402
import soak_data as sd  # noqa: E402

CATEGORIES = ["1st attempt", "2nd attempt", "3rd attempt", "limit reached"]
CATEGORY_COLORS = {"1st attempt": "#2b8a3e", "2nd attempt": "#74b816", "3rd attempt": "#f59f00",
                   "limit reached": "#adb5bd"}
MODES = ["inspection_demo", "reach_cable", "cable_charging", "leave_cable"]
MODE_LABELS = {"inspection_demo": "Inspection", "reach_cable": "Reach Cable",
               "cable_charging": "Cable Charging", "leave_cable": "Leave Cable"}
MODE_COLORS = {"inspection_demo": "#d0ebff", "reach_cable": "#ffe8cc", "cable_charging": "#d3f9d8",
               "leave_cable": "#f3d9fa"}
SINGLE = 3.5   # in, one column
DOUBLE = 7.16  # in, two columns
WORLD_SDF = sd.ROOT / "src/III-Drone-Simulation/Gazebo-simulation-assets/worlds/hca_full_pylon_setup.sdf"
WIND_SEED = (1.75, 0.0)  # m/s, ENU, as set by the soak's wind keeper

plt.rcParams.update({"font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8, "legend.fontsize": 7,
                     "xtick.labelsize": 7, "ytick.labelsize": 7, "figure.dpi": 150, "savefig.bbox": "tight",
                     "pdf.fonttype": 42, "ps.fonttype": 42})

MISSING: list[str] = []


def save(fig, out: pathlib.Path, name: str) -> None:
    for ext in ("pdf", "png"):
        fig.savefig(out / f"{name}.{ext}", dpi=300)
    plt.close(fig)


def write_table(out: pathlib.Path, name: str, header: list[str], rows: list[list], caption: str, notes: list[str]) -> None:
    with open(out / f"{name}.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    md = [f"**{caption}**", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    md += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    md += [""] + [f"{n}" for n in notes]
    (out / f"{name}.md").write_text("\n".join(md) + "\n")
    esc = lambda text: str(text).replace("%", r"\%").replace("&", r"\&").replace("_", r"\_").replace("#", r"\#")  # noqa: E731
    tex = [r"\begin{table}[t]", r"\centering", rf"\caption{{{esc(caption)}}}", rf"\label{{tab:{name}}}",
           r"\begin{tabular}{l" + "r" * (len(header) - 1) + "}", r"\toprule",
           " & ".join(esc(h) for h in header) + r" \\", r"\midrule"]
    for row in rows:
        if row and str(row[0]).startswith("—"):
            tex.append(r"\midrule")
            tex.append(rf"\multicolumn{{{len(header)}}}{{l}}{{\emph{{{esc(str(row[0]).strip('— '))}}}}} \\")
            continue
        tex.append(" & ".join(esc(c) for c in row) + r" \\")
    tex += [r"\bottomrule", r"\end{tabular}"]
    if notes:
        tex.append(r"\par\smallskip\footnotesize " + r" \\ ".join(esc(n) for n in notes))
    tex.append(r"\end{table}")
    (out / f"{name}.tex").write_text("\n".join(tex) + "\n")


# ----------------------------------------------------------------------------- data

class Data:
    def __init__(self, cat: sd.Catalogue) -> None:
        self.cat = cat
        self.logs = {r.run: sd.parse_run(r) for r in cat.runs}
        self.series: dict[int, list[dict]] = {}

    def phases(self, condition: str, outcomes=("captured", "limit")) -> list[sd.LandingPhase]:
        return [p for r in self.cat.by_condition(condition) for p in self.logs[r.run].phases if p.outcome in outcomes]

    def run_series(self, run: sd.Run) -> list[dict]:
        if run.run not in self.series:
            out = []
            for bag_dir in run.bag_dirs():
                try:
                    out.append(bx.run_series(bag_dir, self.cat.cache))
                except Exception as exc:  # noqa: BLE001 - reported in MISSING.md
                    MISSING.append(f"bag {bag_dir.name} (run {run.run}) could not be read: {exc}")
            self.series[run.run] = out
        return self.series[run.run]

    def battery(self, run: sd.Run) -> np.ndarray:
        parts = [s["battery"] for s in self.run_series(run) if len(s["battery"])]
        if not parts:
            return np.zeros((0, 3))
        data = np.concatenate(parts)
        data = data[np.argsort(data[:, 0])]
        return data[(data[:, 0] >= run.start) & (data[:, 0] <= run.end)]


def pct(k: int, n: int) -> str:
    return f"{100.0 * k / n:.1f}" if n else "–"


def ci(k: int, n: int) -> str:
    lo, hi = sd.wilson(k, n)
    return f"[{100 * lo:.1f}, {100 * hi:.1f}]" if n else "–"


# ----------------------------------------------------------------------------- tables

def table1(data: Data, out: pathlib.Path) -> dict:
    cat = data.cat
    cols = ["no_wind", "wind"]
    stats = {}
    for cond in cols:
        runs = cat.by_condition(cond)
        ph = data.phases(cond)
        cats = collections.Counter(p.category for p in ph)
        n = len(ph)
        captured = sum(1 for p in ph if p.outcome == "captured")
        counts = collections.Counter()
        for r in runs:
            counts.update(data.logs[r.run].counts)
        # Cable Charging succeeds only at full charge (battery-driven cycles).
        charges = counts["charging_succeeded"]
        stats[cond] = {
            "runs": ", ".join(str(r.run) for r in runs) + (" (running)" if any(r.running for r in runs) else ""),
            "hours": sum(r.hours for r in runs),
            "cycles": counts["leave_succeeded"],
            "charges": charges,
            "mode_activations": sum(len(data.logs[r.run].modes) for r in runs),
            "n": n, "captured": captured, "first": cats["1st attempt"], "second": cats["2nd attempt"],
            "third": cats["3rd attempt"], "limit": cats["limit reached"],
            "late": sum(p.late_capture for p in ph),
            "attempts": sum(p.attempts for p in ph), "aborts": sum(p.aborts for p in ph),
            "aborts_vgate": sum(p.aborts_vgate for p in ph), "aborts_above": sum(p.aborts_above_target for p in ph),
            "interrupted": sum(1 for r in runs for p in data.logs[r.run].phases if p.outcome == "interrupted"),
            "infrastructure": [i for i in cat.config.get("infrastructure", []) if any(i["run"] == r.run for r in runs)],
        }
    s = stats
    header = ["", "No wind", "Wind (1.75 m/s, gusts)"]
    row = lambda label, key, fmt="{}": [label] + [fmt.format(s[c][key]) for c in cols]  # noqa: E731
    rows = [
        row("Runs", "runs"),
        [ "Autonomous operation (h)"] + [f"{s[c]['hours']:.1f}" for c in cols],
        row("Complete mission cycles", "cycles"),
        row("Charges completed (to full)", "charges"),
        row("Mission mode activations", "mode_activations"),
        ["Human interventions", "0", "0"],
        ["— Landing phases"],
        row("Landing phases", "n"),
        ["Captured within the attempt limit (%)"] + [f"{s[c]['captured']} ({pct(s[c]['captured'], s[c]['n'])}) {ci(s[c]['captured'], s[c]['n'])}" for c in cols],
        ["  at the 1st attempt (%)"] + [f"{s[c]['first']} ({pct(s[c]['first'], s[c]['n'])}) {ci(s[c]['first'], s[c]['n'])}" for c in cols],
        ["  at the 2nd attempt (%)"] + [f"{s[c]['second']} ({pct(s[c]['second'], s[c]['n'])})" for c in cols],
        ["  at the 3rd attempt (%)"] + [f"{s[c]['third']} ({pct(s[c]['third'], s[c]['n'])})" for c in cols],
        ["Attempt limit reached (a) (%)"] + [f"{s[c]['limit']} ({pct(s[c]['limit'], s[c]['n'])})" for c in cols],
        ["Late captures (b)"] + [str(s[c]["late"]) for c in cols],
        ["Landing attempts / aborted"] + [f"{s[c]['attempts']} / {s[c]['aborts']}" for c in cols],
        ["Abort trigger: V-gate / above-target"] + [f"{s[c]['aborts_vgate']} / {s[c]['aborts_above']}" for c in cols],
        ["— Outside the system statistics"],
        ["Infrastructure interruptions (c)"] + [str(len(s[c]["infrastructure"])) for c in cols],
    ]
    notes = [
        "Percentages of landing phases with a decided outcome; brackets: 95 % Wilson confidence interval.",
        f"(a) Attempt limit of {sd.LANDING_ATTEMPT_LIMIT} is a validation setting; the test harness then flies the vehicle back to the mission start pose and resumes.",
        "(b) The landing aborted at contact while the gripper closed; the gripper-closed check continued the mission. Counted as captured.",
        "(c) " + "; ".join(f"run {i['run']}: {i['what']}" for c in cols for i in s[c]["infrastructure"]) + ".",
    ]
    write_table(out, "table1_campaign_summary", header, rows,
                "Simulation campaign summary (HIL, Pi flight computer)", notes)
    return stats


def table2(data: Data, out: pathlib.Path) -> dict:
    cols = ["no_wind", "wind"]
    values = {}
    for cond in cols:
        runs = data.cat.by_condition(cond)
        ph = [p for r in runs for p in data.logs[r.run].phases]
        decided = [p for p in ph if p.outcome in ("captured", "limit")]
        counts = collections.Counter()
        for r in runs:
            counts.update(data.logs[r.run].counts)
        leaves = sum(1 for r in runs for m in data.logs[r.run].modes if m.mode == "leave_cable")
        captured = sum(1 for p in decided if p.outcome == "captured")
        values[cond] = {
            "landing_retry": sum(max(0, p.attempts - 1) for p in decided),
            "retry_takeoff": sum(p.retry_takeoffs for p in ph),
            "retry_takeoff_rejected": sum(p.retry_takeoffs_rejected for p in ph),
            "gripper_checks": sum(p.gripper_checks for p in ph),
            "late": sum(p.late_capture for p in ph),
            "limit": sum(1 for p in decided if p.outcome == "limit"),
            "perception": sum(p.perception_recoveries for p in ph),
            "repoll": sum(p.detection_repolls for p in ph),
            "repoll_phases": sum(1 for p in ph if p.detection_repolls),
            "arm_retry": max(0, counts["arm_request"] - leaves),
            "push_hold": counts["push_not_established"],
            "disarm_retry": max(0, counts["disarm_request"] - captured - counts["push_not_established"]),
            "lock": sum(p.lock_reacquisitions for p in ph),
        }
    v = values
    header = ["Branch (behaviour tree node)", "No wind", "Wind"]
    rows = [
        ["— Reach Cable: landing fallback"],
        ["Landing retried after an abort (landing_maneuver_retry_until_successful_on_aborted)"] + [str(v[c]["landing_retry"]) for c in cols],
        ["Retry take-off after an aborted landing (cable_takeoff_retry); refused (d)"] + [f"{v[c]['retry_takeoff']}; {v[c]['retry_takeoff_rejected']}" for c in cols],
        ["Gripper-closed check after a landing failure; found closed"] + [f"{v[c]['gripper_checks']}; {v[c]['late']}" for c in cols],
        ["Attempt limit reached, Reach Cable fails (test setting)"] + [str(v[c]["limit"]) for c in cols],
        ["— Reach Cable: perception"],
        ["Perception reset when the live cable yaw does not settle (reset_unsettled_perception)"] + [str(v[c]["perception"]) for c in cols],
        ["Powerline detection re-poll before landing (detect_powerline_retry_until_successful); phases"] + [f"{v[c]['repoll']}; {v[c]['repoll_phases']}" for c in cols],
        ["— Leave Cable"],
        ["Arm retry on the cable (arm_on_cable_retry)"] + [str(v[c]["arm_retry"]) for c in cols],
        ["Hold on the cable after a failed push (hold_cable_or_stay_on_it)"] + [str(v[c]["push_hold"]) for c in cols],
        ["Disarm retry on the cable (disarm_on_cable_retry)"] + [str(v[c]["disarm_retry"]) for c in cols],
        ["— Cable landing maneuver (controller level, not a tree branch)"],
        ["Conductor lock re-acquisition events"] + [str(v[c]["lock"]) for c in cols],
    ]
    notes = ["Counts of branch activations from the mission executor and maneuver controller logs.",
             "(d) Refused by the maneuver server: a cable take-off needs an open gripper, and the gripper had closed (late capture)."]
    write_table(out, "table2_branch_coverage", header, rows, "Behaviour-tree fallback and retry branch activations", notes)
    return values


# ----------------------------------------------------------------------------- figures

def fig1(data: Data, out: pathlib.Path) -> None:
    fig, ax = plt.subplots(figsize=(SINGLE, 1.35))
    labels = []
    for i, cond in enumerate(["no_wind", "wind"]):
        ph = data.phases(cond)
        n = len(ph)
        c = collections.Counter(p.category for p in ph)
        left = 0.0
        for cat in CATEGORIES:
            share = 100.0 * c[cat] / n if n else 0.0
            ax.barh(i, share, left=left, color=CATEGORY_COLORS[cat], edgecolor="white", linewidth=0.5,
                    hatch="///" if cat == "limit reached" else None)
            if share >= 6:
                ax.text(left + share / 2, i, f"{share:.0f} %", ha="center", va="center", fontsize=6.5,
                        color="white" if cat in ("1st attempt", "2nd attempt") else "black")
            left += share
        labels.append(f"{data.cat.config['conditions'][cond]['label']}\n(n = {n})")
    ax.set_yticks([0, 1], labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("Landing phases (%)")
    handles = [Patch(facecolor=CATEGORY_COLORS[c], hatch="///" if c == "limit reached" else None,
                     label="limit reached (test setting: 3)" if c == "limit reached" else f"captured, {c}")
               for c in CATEGORIES]
    ax.legend(handles=handles, ncol=2, loc="upper center", bbox_to_anchor=(0.45, -0.55), frameon=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    save(fig, out, "fig1_attempts_to_capture")


def wind_parameters() -> dict:
    plugin = next(p for p in ET.parse(WORLD_SDF).getroot().iter("plugin") if "WindEffects" in (p.get("name") or ""))
    get = lambda path, default: float(plugin.find(path).text) if plugin.find(path) is not None else default  # noqa: E731
    return {"amp": get("horizontal/magnitude/sin/amplitude_percent", 0.0), "period": get("horizontal/magnitude/sin/period", 1.0),
            "dir_amp": get("horizontal/direction/sin/amplitude", 0.0), "dir_period": get("horizontal/direction/sin/period", 1.0)}


def applied_wind_from_clock(sim_time: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """WindEffects' settled gust model (no noise) at the given simulation times:
    speed (m/s) and direction (deg, ENU)."""
    p = wind_parameters()
    base = math.hypot(*WIND_SEED)
    base_dir = math.degrees(math.atan2(WIND_SEED[1], WIND_SEED[0]))
    speed = base * (1 + p["amp"] * np.sin(2 * np.pi * sim_time / p["period"]))
    direction = base_dir + p["dir_amp"] * np.sin(2 * np.pi * sim_time / p["dir_period"])
    return speed, direction


def choose_recovered_landing(data: Data) -> tuple[sd.Run, sd.LandingPhase] | None:
    preferred = [data.cat.config["figure_runs"]["wind"]] + [r.run for r in reversed(data.cat.by_condition("wind"))]
    seen = set()
    for number in preferred:
        if number in seen:
            continue
        seen.add(number)
        run = data.cat.run(number)
        if not run.bag_dirs():
            continue
        for p in data.logs[number].phases:
            if p.outcome == "captured" and p.attempts == 2 and not p.late_capture and p.retry_takeoffs == 1:
                return run, p
    return None


def fig2(data: Data, out: pathlib.Path) -> dict:
    chosen = choose_recovered_landing(data)
    if chosen is None:
        MISSING.append("Figure 2: no recovered landing (2 attempts, captured) with a bag in the wind runs.")
        return {}
    run, phase = chosen
    log = data.logs[run.run]
    goals = [t for t, _l, m in log.lines if phase.start <= t <= phase.end and sd.SIG["landing_goal"] in m]
    t_ref = goals[0]
    t0, t1 = t_ref - 15.0, phase.end + 5.0
    nodes = [n for n in sd.node_intervals(log.lines, t0, t1)]
    checks = [(t, sd.SIG["gripper_closed"] in m) for t, _l, m in log.lines if t0 <= t <= t1
              and (sd.SIG["gripper_closed"] in m or sd.SIG["gripper_not_closed"] in m)]
    bag_dirs = [d for d in run.bag_dirs()]
    try:
        sig = bx.window_signals(bag_dirs, t0, t1, data.cat.cache)
    except Exception as exc:  # noqa: BLE001
        MISSING.append(f"Figure 2: bag window of run {run.run} could not be read ({exc}); trace drawn without signals.")
        sig = {}

    fig, axes = plt.subplots(4, 1, figsize=(DOUBLE, 5.0), sharex=True, layout="constrained",
                             gridspec_kw={"height_ratios": [2.4, 1, 1, 0.5]})
    ax = axes[0]
    names = []
    for n in nodes:
        if n["node"] not in names:
            names.append(n["node"])
    result_colors = {"succeeded": "#2b8a3e", "failed": "#e03131", "rejected": "#f59f00", "running": "#868e96"}
    for n in nodes:
        y = names.index(n["node"])
        ax.barh(y, max(0.3, n["end"] - n["start"]), left=n["start"] - t_ref, height=0.6,
                color=result_colors[n["result"]], edgecolor="none")
    if checks:
        y = len(names)
        names.append("verify_gripper_closed (check)")
        for t, closed in checks:
            ax.plot(t - t_ref, y, marker="|", color="#2b8a3e" if closed else "#e03131", markersize=6)
    ax.set_yticks(range(len(names)), names)
    ax.invert_yaxis()
    ax.legend(handles=[Patch(color=c, label=k) for k, c in result_colors.items() if k != "running"],
              loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=3, frameon=False)
    ax.set_title(f"run {run.run}, {dt.datetime.fromtimestamp(t_ref, dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
                 loc="right", fontsize=7)

    ax = axes[1]
    wind_note = ""
    if sig.get("wind") is not None and len(sig["wind"]):
        w = sig["wind"]
        speed = np.hypot(w[:, 1], w[:, 2])
        direction = np.degrees(np.arctan2(w[:, 2], w[:, 1]))
        tw = w[:, 0]
        wind_note = "logged"
    elif sig.get("clock") is not None and len(sig["clock"]):
        c = sig["clock"]
        speed, direction = applied_wind_from_clock(c[:, 1])
        tw = c[:, 0]
        wind_note = "reconstructed from /clock (WindEffects gust model)"
    else:
        tw = np.zeros(0)
        speed = direction = np.zeros(0)
        MISSING.append("Figure 2: no wind or clock samples in the window.")
    if tw.size:
        ax.plot(tw - t_ref, speed, color="#1971c2", lw=0.9)
        ax2 = ax.twinx()
        ax2.plot(tw - t_ref, direction, color="#e8590c", lw=0.7, ls="--")
        ax.set_ylabel("wind (m/s)\nsolid")
        ax2.set_ylabel("direction (deg)\ndashed", color="#e8590c")

    ax = axes[2]
    deviation_note = None
    ref, odo = sig.get("reference"), sig.get("odometry")
    if ref is not None and odo is not None and len(ref) and len(odo):
        # The controller reference is in the III world frame (north, west, up);
        # PX4 odometry is north, east, down. References with unused (NaN)
        # position fields are left out.
        ref = ref[np.all(np.isfinite(ref[:, 1:4]), axis=1)]
        o_t = odo[:, 0]
        nwu = np.column_stack([odo[:, 1], -odo[:, 2], -odo[:, 3]])
        idx = np.clip(np.searchsorted(o_t, ref[:, 0]), 0, len(o_t) - 1)
        err = np.linalg.norm(ref[:, 1:4] - nwu[idx], axis=1)
        if len(err) and np.nanmedian(err) < 0.5:
            # Break the line where the reference has no position (cable landing).
            tt = ref[:, 0] - t_ref
            yy = err * 100
            breaks = np.where(np.diff(tt) > 0.5)[0]
            tt = np.insert(tt, breaks + 1, np.nan)
            yy = np.insert(yy, breaks + 1, np.nan)
            ax.plot(tt, yy, color="#5f3dc4", lw=0.8)
            ax.set_ylabel("|ref − pos| (cm)")
            deviation_note = "controller reference vs PX4 odometry"
    if deviation_note is None:
        truth = sig.get("truth")
        if truth is not None and len(truth):
            ax.plot(truth[:, 0] - t_ref, truth[:, 3], color="#5f3dc4", lw=0.8)
            ax.set_ylabel("height (m)")
            MISSING.append("Figure 2: controller reference and PX4 odometry frames did not line up (median "
                           "deviation >= 0.5 m); panel shows ground-truth height instead of tracking deviation.")

    ax = axes[3]
    grip = sig.get("gripper")
    if grip is not None and len(grip):
        ax.step(grip[:, 0] - t_ref, grip[:, 1], where="post", color="#212529", lw=0.8)
    ax.set_yticks([0, 1], ["open", "closed"])
    ax.set_ylim(-0.3, 1.3)
    ax.set_xlabel("time from the first landing attempt (s)")
    for a in axes:
        for side in ("top",):
            a.spines[side].set_visible(False)
    if wind_note:
        axes[1].text(1.0, 1.02, f"applied wind, {wind_note}", transform=axes[1].transAxes, ha="right", fontsize=6.5)
    if deviation_note:
        axes[2].text(1.0, 1.02, f"deviation from the controller reference ({deviation_note})",
                     transform=axes[2].transAxes, ha="right", fontsize=6.5)
    save(fig, out, "fig2_recovered_landing_trace")
    return {"run": run.run, "phase_start": phase.start, "wind": wind_note, "deviation": deviation_note}


def fig3(data: Data, out: pathlib.Path) -> None:
    runs = data.cat.by_condition("no_wind") + data.cat.by_condition("wind")
    fig, ax = plt.subplots(figsize=(DOUBLE, 0.32 * len(runs) + 0.9))
    for y, run in enumerate(runs):
        ax.plot([0, run.hours], [y, y], color="#dee2e6", lw=6, solid_capstyle="butt", zorder=1)
        for p in data.logs[run.run].phases:
            h = (p.start - run.start) / 3600.0
            if p.outcome == "captured":
                ax.plot([h, h], [y - 0.28, y + 0.28], color=CATEGORY_COLORS[p.category], lw=0.9, zorder=2)
            elif p.outcome == "limit":
                ax.plot(h, y, marker="x", color="#212529", markersize=3.5, mew=0.8, zorder=3)
            elif p.outcome == "interrupted":
                ax.plot(h, y, marker="v", color="#c92a2a", markersize=3.5, zorder=3)
        if run.running:
            ax.annotate("running", (run.hours, y), xytext=(3, 0), textcoords="offset points", va="center", fontsize=6)
        elif any(i["run"] == run.run for i in data.cat.config.get("infrastructure", [])) and not any(
                p.outcome == "interrupted" for p in data.logs[run.run].phases):
            ax.plot(run.hours, y, marker="v", color="#c92a2a", markersize=3.5, zorder=3)
    ax.set_yticks(range(len(runs)), [f"{'No wind' if r.condition == 'no_wind' else 'Wind'}, run {r.run}" for r in runs])
    ax.invert_yaxis()
    ax.set_xlabel("time since run start (h)")
    handles = [plt.Line2D([], [], color=CATEGORY_COLORS[c], lw=1.5, label=f"captured, {c}") for c in CATEGORIES[:3]]
    handles += [plt.Line2D([], [], color="#212529", marker="x", lw=0, label="limit reached (test setting)"),
                plt.Line2D([], [], color="#c92a2a", marker="v", lw=0, label="infrastructure interruption")]
    ax.legend(handles=handles, ncol=5, loc="upper center", bbox_to_anchor=(0.5, -0.22), frameon=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    save(fig, out, "fig3_run_timelines")


def shade_modes(ax, modes: list[sd.ModeInterval], t0: float, t1: float, offset: float, scale: float) -> None:
    for m in modes:
        if m.end < t0 or m.start > t1 or m.mode not in MODE_COLORS:
            continue
        ax.axvspan((max(m.start, t0) - offset) / scale, (min(m.end, t1) - offset) / scale,
                   color=MODE_COLORS[m.mode], lw=0, zorder=0)


def zoom_window(log: sd.RunLog, want_retry: bool) -> tuple[float, float] | None:
    cycles = log.cycles()
    for a, b in zip(cycles, cycles[1:]):
        pa, pb = a["phase"], b["phase"]
        if not pa or not pb or pa.outcome != "captured" or pb.outcome != "captured":
            continue
        if want_retry and pa.attempts < 2 and pb.attempts < 2:
            continue
        if not want_retry and (pa.attempts > 1 or pb.attempts > 1):
            continue
        return a["start"], b["end"]
    return None


def fig4(data: Data, out: pathlib.Path) -> None:
    nominal = data.cat.run(data.cat.config["figure_runs"]["no_wind"])
    windy = data.cat.run(data.cat.config["figure_runs"]["wind"])
    bat_n, bat_w = data.battery(nominal), data.battery(windy)
    if not len(bat_n):
        MISSING.append(f"Figure 4: no battery samples for run {nominal.run} (bag cache missing?).")
        return
    fig = plt.figure(figsize=(DOUBLE, 4.2))
    grid = fig.add_gridspec(2, 2, height_ratios=[1, 1.1])
    ax = fig.add_subplot(grid[0, :])
    hours = (bat_n[:, 0] - nominal.start) / 3600
    ax.plot(hours, bat_n[:, 1], color="#1971c2", lw=0.4)
    triggers = [m.start for m in data.logs[nominal.run].modes if m.mode == "reach_cable"]
    trig_v = np.interp(triggers, bat_n[:, 0], bat_n[:, 1])
    ax.plot((np.asarray(triggers) - nominal.start) / 3600, trig_v, "|", color="#e8590c", markersize=3, mew=0.5, lw=0,
            label="recharge triggered (Reach Cable starts)")
    ax.set_xlabel(f"time since run start (h), no wind, run {nominal.run}")
    ax.set_ylabel("battery voltage (V)")
    ax.legend(loc="lower right", frameon=False)
    ax.set_title("Battery voltage over the whole run", loc="left")
    for col, (run, bat, retry, title) in enumerate([
            (nominal, bat_n, False, f"Two cycles, no wind (run {nominal.run})"),
            (windy, bat_w, True, f"Two cycles with a landing retry, wind (run {windy.run})")]):
        ax = fig.add_subplot(grid[1, col])
        window = zoom_window(data.logs[run.run], retry)
        if window is None or not len(bat):
            ax.text(0.5, 0.5, "no suitable cycles or battery data", ha="center", va="center", transform=ax.transAxes)
            MISSING.append(f"Figure 4: zoom for run {run.run} not available.")
            continue
        t0, t1 = window
        sel = (bat[:, 0] >= t0) & (bat[:, 0] <= t1)
        shade_modes(ax, data.logs[run.run].modes, t0, t1, t0, 60)
        ax.plot((bat[sel, 0] - t0) / 60, bat[sel, 1], color="#1971c2", lw=0.8)
        for p in data.logs[run.run].phases:
            if t0 <= p.start <= t1 and p.attempts >= 2:
                retries = [t for t, _l, m in data.logs[run.run].lines if p.start <= t <= p.end and sd.SIG["retry_takeoff"] in m]
                for t in retries:
                    ax.axvline((t - t0) / 60, color="#e03131", lw=0.7, ls="--")
        ax.set_xlabel("time (min)")
        ax.set_ylabel("battery voltage (V)")
        ax.set_title(title, loc="left")
    handles = [Patch(color=MODE_COLORS[m], label=MODE_LABELS[m]) for m in MODES]
    handles.append(plt.Line2D([], [], color="#e03131", ls="--", lw=0.8, label="retry take-off after an aborted landing"))
    fig.legend(handles=handles, ncol=5, loc="lower center", bbox_to_anchor=(0.5, -0.04), frameon=False)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    save(fig, out, "fig4_battery_and_modes")


def fig5(data: Data, out: pathlib.Path) -> dict:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(DOUBLE, 2.3), gridspec_kw={"width_ratios": [1, 1.4]})
    cycle_minutes = {}
    for cond in ("no_wind", "wind"):
        values = []
        for r in data.cat.by_condition(cond):
            for c in data.logs[r.run].cycles():
                if c["phase"] is not None and c["phase"].outcome == "captured":
                    values.append(c["duration_s"] / 60)
        cycle_minutes[cond] = values
    ax1.boxplot([cycle_minutes["no_wind"], cycle_minutes["wind"]], widths=0.5, showfliers=True,
                flierprops={"markersize": 2}, medianprops={"color": "#e8590c"})
    ax1.set_xticks([1, 2], [f"No wind\n(n = {len(cycle_minutes['no_wind'])})", f"Wind\n(n = {len(cycle_minutes['wind'])})"])
    ax1.set_ylabel("mission cycle time (min)")
    ax1.set_title("Cycle time (captured cycles)", loc="left")
    by_attempts = collections.defaultdict(list)
    for cond in ("no_wind", "wind"):
        for p in data.phases(cond, outcomes=("captured",)):
            by_attempts[p.attempts].append((p.end - p.start) / 60)
    keys = sorted(k for k in by_attempts if k <= 3)
    ax2.boxplot([by_attempts[k] for k in keys], widths=0.5, flierprops={"markersize": 2}, medianprops={"color": "#e8590c"})
    ax2.set_xticks(range(1, len(keys) + 1), [f"{k} attempt{'s' if k > 1 else ''}\n(n = {len(by_attempts[k])})" for k in keys])
    ax2.set_ylabel("landing phase duration (min)")
    medians = {k: statistics.median(by_attempts[k]) for k in keys}
    added = (medians[2] - medians[1]) * 60 if 1 in medians and 2 in medians else float("nan")
    ax2.set_title(f"Reach Cable duration by attempts (median +{added:.0f} s per retry)", loc="left")
    for a in (ax1, ax2):
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
    fig.tight_layout()
    save(fig, out, "fig5_cycle_time")
    return {"cycle_median_min": {c: statistics.median(v) for c, v in cycle_minutes.items() if v},
            "landing_median_min_by_attempts": medians, "seconds_added_per_retry": added}


def fig6(data: Data, out: pathlib.Path) -> dict:
    runs = [data.cat.run(data.cat.config["figure_runs"][c]) for c in ("no_wind", "wind")]
    cpu = {r.run: sd.read_tsv(r.pi_cpu, r.run) for r in runs}
    mem = {r.run: sd.read_tsv(r.pi_mem, r.run) for r in runs}
    rtf = {r.run: sd.read_rtf(r.rtf, r.run) for r in runs}
    procs = collections.Counter()
    for r in runs:
        for name, rows in cpu[r.run].items():
            if not name.startswith("__"):
                procs[name] += statistics.mean(v for _t, v in rows)
    top = [name for name, _v in procs.most_common(6)]
    fig, axes = plt.subplots(4, 2, figsize=(DOUBLE, 6.2), sharex="col", sharey="row", layout="constrained")
    colors = plt.get_cmap("tab10").colors
    summary = {}
    for col, run in enumerate(runs):
        h = lambda t: (np.asarray(t) - run.start) / 3600  # noqa: E731
        label = f"{'No wind' if run.condition == 'no_wind' else 'Wind'}, run {run.run}"
        ax = axes[0, col]
        total = cpu[run.run].get("__total__", [])
        if total:
            times = [t for t, _v in total]
            stack, labels = [], []
            for name in top:
                series = dict(cpu[run.run].get(name, []))
                stack.append([series.get(t, 0.0) for t in times])
                labels.append(name)
            other = [tot - sum(s[i] for s in stack) for i, (_t, tot) in enumerate(total)]
            stack.append([max(0.0, v) for v in other])
            labels.append("other")
            ax.stackplot(h(times), stack, labels=labels, colors=list(colors[:len(top)]) + ["#ced4da"], lw=0)
            summary[run.run] = {"cpu_total_mean": statistics.mean(v for _t, v in total)}
        else:
            MISSING.append(f"Figure 6: no Pi CPU samples for run {run.run}.")
        ax.set_title(label, loc="left")
        if col == 0:
            ax.set_ylabel("Pi CPU (% of one core)")
        ax = axes[1, col]
        load = cpu[run.run].get("__load1__", [])
        psi = cpu[run.run].get("__psi_some_avg60__", [])
        if load:
            ax.plot(h([t for t, _v in load]), [v for _t, v in load], color="#1971c2", lw=0.8, label="load average (1 min)")
        if psi:
            ax.plot(h([t for t, _v in psi]), [v / 10 for _t, v in psi], color="#e8590c", lw=0.8, label="CPU pressure stall (%) / 10")
        if col == 0:
            ax.set_ylabel("load / pressure")
        ax = axes[2, col]
        used = mem[run.run].get("__mem_used_mb__", [])
        if used:
            ax.plot(h([t for t, _v in used]), [v / 1024 for _t, v in used], color="#5f3dc4", lw=0.8)
            summary.setdefault(run.run, {})["mem_used_gib_max"] = max(v for _t, v in used) / 1024
        else:
            ax.text(0.5, 0.5, "memory not recorded for this run", ha="center", va="center", transform=ax.transAxes,
                    fontsize=7, color="#868e96")
            ax.set_yticks([])
            MISSING.append(f"Figure 6: Pi memory not recorded for run {run.run} (memory logging starts with run 47).")
        if col == 0:
            ax.set_ylabel("Pi memory used (GiB)")
        ax = axes[3, col]
        if rtf[run.run]:
            t = h([r[0] for r in rtf[run.run]])
            ax.plot(t, [r[2] for r in rtf[run.run]], color="#2b8a3e", lw=0.8, label="60 s")
            ax.plot(t, [r[1] for r in rtf[run.run]], color="#e03131", lw=0.5, alpha=0.8, label="worst 5 s window")
            summary.setdefault(run.run, {})["rtf_min_5s"] = min(r[1] for r in rtf[run.run])
        if col == 0:
            ax.set_ylabel("Gazebo real-time factor")
        ax.set_xlabel("time since run start (h)")
        series = data.run_series(run)
        if series:
            q = np.nanmax(np.vstack([s["gap_quantiles"] for s in series]), axis=0)
            med = np.nanmedian(np.vstack([s["gap_quantiles"] for s in series])[:, 0])
            over = np.sum(np.vstack([s["gap_over"] for s in series]), axis=0)
            count = int(sum(int(s["odometry_count"]) for s in series))
            text = (f"PX4 odometry received at the Pi: {count / 1e6:.1f} M messages\n"
                    f"inter-arrival median {med * 1e3:.0f} ms, p99 {q[2] * 1e3:.0f} ms, max {q[4] * 1e3:.0f} ms, "
                    f"gaps > 250 ms: {int(over[2])}")
            axes[3, col].text(0.0, -0.55, text, transform=axes[3, col].transAxes, fontsize=6.5, va="top")
            summary.setdefault(run.run, {}).update({"odometry_msgs": count, "gap_median_ms": med * 1e3,
                                                    "gap_p99_ms_max": q[2] * 1e3, "gap_max_ms": q[4] * 1e3,
                                                    "gaps_over_250ms": int(over[2])})
        else:
            MISSING.append(f"Figure 6: no bag cache for run {run.run}; odometry inter-arrival statistics missing.")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=4, frameon=False, fontsize=6.5)
    for row in (1, 3):
        if axes[row, 0].get_legend_handles_labels()[0]:
            axes[row, 0].legend(loc="lower left", ncol=2, frameon=False, fontsize=6)
    axes[3, 0].set_ylim(0.8, 1.05)
    for a in axes.flat:
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
    save(fig, out, "fig6_computational_load")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=pathlib.Path, default=None, help="run catalogue (default runs.yaml)")
    args = parser.parse_args()
    cat = sd.load_catalogue(args.runs)
    out = cat.output
    out.mkdir(parents=True, exist_ok=True)
    data = Data(cat)
    summary = {"built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "table1": table1(data, out), "table2": table2(data, out)}
    fig1(data, out)
    summary["fig2"] = fig2(data, out)
    fig3(data, out)
    fig4(data, out)
    summary["fig5"] = fig5(data, out)
    summary["fig6"] = fig6(data, out)
    running = [r.run for r in cat.runs if r.running]
    if running:
        MISSING.append(f"Run(s) {running} still running: their numbers cover the data up to {summary['built_at']}.")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    (out / "MISSING.md").write_text("# Not built or incomplete\n\n" + ("\n".join(f"- {m}" for m in dict.fromkeys(MISSING)) or "- nothing") + "\n")
    print(f"wrote {out}")
    for m in dict.fromkeys(MISSING):
        print(f"MISSING: {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
