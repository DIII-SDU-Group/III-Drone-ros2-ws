#!/bin/bash

packages_names=""

for file in $( find src/ -name package.xml ); do
    package_name=$( grep -oPm1 "(?<=<name>)[^<]+" $file )
    # If package name start with iii, add it to the list
    if [[ $package_name == iii* ]]; then
        packages_names="$packages_names $package_name"
    fi
done

# Echo each pacakge name on a new line
echo $packages_names | tr " " "\n"

