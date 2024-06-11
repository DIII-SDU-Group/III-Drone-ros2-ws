#!/bin/bash

SYSROOT=$1
TARGET_DIR=$2

find $TARGET_DIR -type l | while read link; do
    # Get the target of the symbolic link
    target=$(readlink "$link")
    
    # If the target is an absolute path, update it to point within the sysroot
    if [[ "$target" == /* ]]; then
        new_target="$SYSROOT$target"
        
        # Get the original permissions and ownership
        permissions=$(stat -c "%a" "$link")
        owner=$(stat -c "%u" "$link")
        group=$(stat -c "%g" "$link")

        # Create a new symbolic link with the updated target
        ln -sf "$new_target" "$link"

        # Set the original permissions and ownership
        chown --reference="$link" "$link"
        chmod "$permissions" "$link"
        echo "Updated $link -> $new_target with permissions $permissions and ownership $owner:$group"
    fi
done
