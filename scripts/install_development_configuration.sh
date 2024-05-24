#!/bin/sh

set -e

SCRIPT_DIR=$(dirname $0)
WORKSPACE_DIR=$(cd $SCRIPT_DIR/.. && pwd)
CONFIG_DIR=$WORKSPACE_DIR/src/III-Drone-Configuration/config

# Get target config dir from the first argument
if [ -n "$1" ]; then
    target_config_dir=$1
else
    echo "No target config directory specified. Exiting."
    exit 1
fi

mkdir -p $target_config_dir/iii_drone/parameters/

if [ ! -f $target_config_dir/iii_drone/parameters/parameters.yaml ]; then
    cp $CONFIG_DIR/parameters.yaml $target_config_dir/iii_drone/parameters/parameters.yaml
else
    $SCRIPT_DIR/update_installed_parameters.py $CONFIG_DIR/parameters.yaml $target_config_dir/iii_drone/parameters/
fi

cp -f $CONFIG_DIR/ros_params.yaml $target_config_dir/iii_drone/ros_params.yaml
rm -rf $target_config_dir/iii_drone/node_parameters 2> /dev/null
cp -rf $CONFIG_DIR/node_parameters $target_config_dir/iii_drone/