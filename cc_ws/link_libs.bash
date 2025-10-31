#!/bin/bash

SYSROOT=$1
TARGET_LIB_DIR=$2

SYSROOT_TARGET_LIB_DIR=$SYSROOT/$TARGET_LIB_DIR
ROOT_TARGET_LIB_DIR=/$TARGET_LIB_DIR

for f in $(ls $SYSROOT_TARGET_LIB_DIR); do
    # Check if file exists in the root lib directory
    if [ ! -f $ROOT_TARGET_LIB_DIR/$f ]; then
        # Create a link to the file in the root lib directory
        ln -s $SYSROOT_TARGET_LIB_DIR/$f $ROOT_TARGET_LIB_DIR/$f
    fi
done
