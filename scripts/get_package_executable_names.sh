#!/bin/bash

# Verify that the user has provided a package name
if [ $# -ne 1 ]; then
    echo "Usage: $0 <package_name>"
    exit 1
fi

package_name=$1

executables=$(ros2 pkg executables $package_name)

# Remove all occurences of "$package_name " from the list of executables
executables=$(echo "$executables" | sed "s/$package_name //g")

echo "$executables"

