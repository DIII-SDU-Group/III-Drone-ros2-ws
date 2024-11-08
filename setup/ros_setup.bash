SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

#export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

export CYCLONEDDS_URI_REAL=$SCRIPT_DIR/cyclonedds_real.xml
export CYCLONEDDS_URI_REMOTE=$SCRIPT_DIR/cyclonedds_remote.xml