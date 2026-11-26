"""Figure and table style matching the manuscript (TFR revised manuscript).

The manuscript's diagrams (draw.io, Helvetica) use light-blue boxes
(#E2EFF9) with slate-blue outlines (#64859C, #476170, #526874) and dark
slate text (#203746); sand for simulation and specification elements
(#FCEDD6 fill, #A78854 outline, #774209 text); grey for neutral elements
(#F5F5F5, #626F77). Section headings use NavyBlue (#00567D). Tables have a
light-blue sans-serif bold header row (#E2EFF9, text #203746), slate-blue
top and bottom rules (#64859C), light inner rules (#CFD9DE), 8.3 pt text.
"""

from __future__ import annotations

import matplotlib.pyplot as plt

# Manuscript colours
NAVY = "#00567D"
SLATE_DARK = "#476170"
SLATE = "#64859C"
SLATE_LIGHT = "#A9BFCE"
BLUE_FILL = "#E2EFF9"
BLUE_MID = "#C3D8EA"
TEXT = "#203746"
TEXT_DARK = "#172D3C"
SAND = "#A78854"
SAND_DARK = "#774209"
SAND_FILL = "#FCEDD6"
SAND_MID = "#E3C48F"
GREY = "#626F77"
GREY_LIGHT = "#ADBCC5"
GREY_FILL = "#F5F5F5"
RULE_LIGHT = "#CFD9DE"
# The manuscript has no red; failures and interruptions use a muted brick
# that sits with the sand accent.
BRICK = "#A4473A"

# Landing outcome (ordinal): first attempt darkest.
CATEGORY_COLORS = {"1st attempt": NAVY, "2nd attempt": SLATE, "3rd attempt": SAND, "limit reached": GREY_LIGHT}
# Mission modes (background bands), from the manuscript's box fills.
MODE_COLORS = {"inspection_demo": BLUE_FILL, "reach_cable": SAND_FILL, "cable_charging": "#E4E9EC",
               "leave_cable": BLUE_MID}
RESULT_COLORS = {"succeeded": SLATE, "failed": BRICK, "rejected": SAND, "running": GREY_LIGHT}
# Stacked series (Pi processes), blues then sands, "other" in light grey.
SERIES = [NAVY, "#2F7299", SLATE, SLATE_LIGHT, SAND, SAND_MID]
OTHER = "#DDE3E7"

SINGLE = 3.5   # in, one column
DOUBLE = 7.16  # in, two columns


def apply() -> None:
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica", "Nimbus Sans", "Liberation Sans", "Arial", "DejaVu Sans"],
        "font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8, "legend.fontsize": 7,
        "xtick.labelsize": 7, "ytick.labelsize": 7,
        "text.color": TEXT, "axes.labelcolor": TEXT, "axes.titlecolor": TEXT, "axes.titleweight": "bold",
        "axes.edgecolor": SLATE_DARK, "axes.linewidth": 0.6,
        "xtick.color": SLATE_DARK, "ytick.color": SLATE_DARK, "xtick.labelcolor": TEXT, "ytick.labelcolor": TEXT,
        "xtick.major.width": 0.6, "ytick.major.width": 0.6,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": False, "legend.frameon": False,
        "lines.linewidth": 1.0, "patch.linewidth": 0.5,
        "boxplot.boxprops.color": SLATE_DARK, "boxplot.whiskerprops.color": SLATE_DARK,
        "boxplot.capprops.color": SLATE_DARK, "boxplot.medianprops.color": SAND,
        "boxplot.flierprops.markeredgecolor": SLATE, "boxplot.flierprops.markersize": 2,
        "figure.dpi": 150, "savefig.bbox": "tight", "savefig.dpi": 300,
        "pdf.fonttype": 42, "ps.fonttype": 42,
    })


def latex_table(caption: str, label: str, header: list[str], rows: list[list], notes: list[str],
                escape, first_width: float = 0.45) -> str:
    """LaTeX in the manuscript's table style (needs xcolor with `table` and the
    array package, as root.tex loads). A two-column float (table*): the first
    column wraps at `first_width` of the text width."""
    ncol = len(header)
    first = rf">{{\raggedright\arraybackslash}}p{{{first_width}\textwidth}}"
    lines = [r"\begin{table*}[!t]", r"\centering", rf"\caption{{{escape(caption)}}}", rf"\label{{tab:{label}}}",
             r"\fontsize{8.3}{10.2}\selectfont", r"\setlength{\tabcolsep}{3.5pt}", r"\renewcommand{\arraystretch}{1.12}",
             r"\begin{tabular}{" + first + "r" * (ncol - 1) + "}",
             r"\arrayrulecolor[HTML]{64859C}\hline", r"\rowcolor[HTML]{E2EFF9}",
             " & ".join(rf"\rule[-3pt]{{0pt}}{{13pt}}\textcolor[HTML]{{203746}}{{\sffamily\bfseries {escape(h)}}}"
                        for h in header) + r" \\",
             r"\noalign{\vskip 2pt}"]
    for row in rows:
        if row and str(row[0]).startswith("—"):
            lines.append(r"\arrayrulecolor[HTML]{CFD9DE}\hline\noalign{\vskip 2pt}")
            lines.append(rf"\multicolumn{{{ncol}}}{{l}}{{\textcolor[HTML]{{203746}}{{\sffamily\bfseries "
                         rf"{escape(str(row[0]).strip('— '))}}}}} \\")
            continue
        cells = [escape(c) for c in row]
        if str(row[0]).startswith("  "):
            cells[0] = r"\quad " + cells[0].lstrip()
        lines.append(" & ".join(cells) + r" \\")
    lines += [r"\noalign{\vskip 2pt}", r"\arrayrulecolor[HTML]{64859C}\hline", r"\end{tabular}", r"\arrayrulecolor{black}"]
    if notes:
        lines.append(r"\par\smallskip\parbox{\textwidth}{\raggedright\footnotesize " + r"\\ ".join(escape(n) for n in notes) + "}")
    lines.append(r"\end{table*}")
    return "\n".join(lines) + "\n"
