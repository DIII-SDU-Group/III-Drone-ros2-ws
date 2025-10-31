#!/bin/bash

package_name=$1
executable=$2

pid=$(pgrep -f "$WORKSPACE_DIR/install/$package_name/lib/$package_name/$executable")

echo $pid