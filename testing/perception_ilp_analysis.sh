#!/bin/bash

base_dir=rosbags20241120/perception
case=$1
bag_number=3

./perception_ilp_analysis.py \
    --pl_csv $base_dir/exported_bags/$case/$bag_number/perception__pl_mapper__powerline.csv \
    --tf_csv $base_dir/exported_bags/$case/$bag_number/tf.csv -o $base_dir/plots/$case/