#!/bin/bash

SYSROOT=$1
TARGET_INCLUDE_DIR=$2

SYSROOT_TARGET_INCLUDE_DIR=$SYSROOT/$TARGET_INCLUDE_DIR
ROOT_TARGET_INCLUDE_DIR=/$TARGET_INCLUDE_DIR

for d in $(ls $SYSROOT_TARGET_INCLUDE_DIR); do
    if [ -d $SYSROOT_TARGET_INCLUDE_DIR/$d ]; then
        # Check if directory exists in the root include directory
        if [ ! -d $ROOT_TARGET_INCLUDE_DIR/$d ]; then
            # Create a link to the directory in the root include directory
            ln -s $SYSROOT_TARGET_INCLUDE_DIR/$d $ROOT_TARGET_INCLUDE_DIR/$d
        fi
    fi
done
