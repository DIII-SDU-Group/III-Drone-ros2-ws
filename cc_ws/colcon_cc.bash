#!/bin/bash

SCRIPT_DIR=$(dirname $(readlink -f $BASH_SOURCE))

# Set the sysroot and related paths
export CMAKE_SYSROOT=/arm64-sysroot
export CMAKE_C_COMPILER=/usr/bin/aarch64-linux-gnu-gcc
export CMAKE_CXX_COMPILER=/usr/bin/aarch64-linux-gnu-g++
export CMAKE_MAKE_PROGRAM=/usr/bin/make

# Set paths for libraries and includes
export CMAKE_PREFIX_PATH=${CMAKE_SYSROOT}/usr:${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu:${CMAKE_SYSROOT}/home/iii/ws/install
export CMAKE_LIBRARY_PATH=${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu
export CMAKE_INCLUDE_PATH=${CMAKE_SYSROOT}/usr/include

colcon_args=$@

colcon build \
    --packages-skip micro_ros_agent microxrcedds_agent micro_ros_msgs px4_msgs iii_drone_interfaces \
    --packages-skip-regex example_* $colcon_args \
    --cmake-args \
    -DCMAKE_TOOLCHAIN_FILE=/home/iii/ws/arm64-toolchain.cmake \
    -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH} \
    -DCMAKE_LIBRARY_PATH=${CMAKE_LIBRARY_PATH} \
    -DCMAKE_INCLUDE_PATH=${CMAKE_INCLUDE_PATH}

    # --cmake-clean-cache \
    # --cmake-force-configure \