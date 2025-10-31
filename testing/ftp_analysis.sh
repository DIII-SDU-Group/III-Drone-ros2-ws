#!/bin/bash

base_dir=rosbags20241114
ol_dir=$base_dir/ftp_mpc_nsf
cl_dir=$base_dir/ftp_mpc_sf
interp_dir=$base_dir/ftp_interp
out_dir=$base_dir/plots/ftp_analysis

./ftp_analysis.py \
    --open-loop-mpc-dir $ol_dir \
    --closed-loop-mpc-dir $cl_dir \
    --quintic-interpolation-dir $interp_dir \
    --results-output-dir $out_dir #--no-transform