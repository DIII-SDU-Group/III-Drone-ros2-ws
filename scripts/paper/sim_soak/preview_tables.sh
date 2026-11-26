#!/usr/bin/env bash
# Compile the generated tables with the manuscript's document class into
# tables_preview.pdf next to them. usage: preview_tables.sh [manuscript dir]
set -euo pipefail
W=$(cd "$(dirname "$0")/../../.." && pwd)
MS=${1:-/home/ffn/Nextcloud/TFR}
OUT=$W/runtime/paper/sim_soak
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cp "$MS/ieeetj.cls" "$OUT"/table*.tex "$TMP"/
cat > "$TMP/tables_preview.tex" <<'TEX'
\documentclass{ieeetj}
\providecommand{\seclogo}{}
\usepackage{graphicx}
\usepackage{array}
\usepackage[table,xcdraw]{xcolor}
\begin{document}
\input{table1_campaign_summary.tex}
\input{table2_branch_coverage.tex}
\end{document}
TEX
(cd "$TMP" && pdflatex -interaction=nonstopmode tables_preview.tex >/dev/null) || { echo "pdflatex failed; see $TMP"; exit 1; }
cp "$TMP/tables_preview.pdf" "$OUT/"
pdftoppm -r 150 -png -singlefile "$OUT/tables_preview.pdf" "$OUT/tables_preview"
echo "wrote $OUT/tables_preview.pdf"
