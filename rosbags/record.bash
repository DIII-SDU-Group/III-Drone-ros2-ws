#!/bin/bash

script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

topics_file="$script_dir/topics.bash"

# Get list of topics separated by space (in file is separated by newline), skip comments
topics=$(grep -v '^#' $topics_file | tr '\n' ' ')

ros2 bag record $topics