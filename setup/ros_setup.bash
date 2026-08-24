SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
# export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

# FastDDS shared-memory transport is fragile across the devcontainer/host
# process mix used by PX4, Gazebo, supervision, and agent tooling. Prefer
# loopback/subnet UDP discovery and data transport by default so supervised
# ROS graph readiness does not depend on stale SHM segments.
export FASTDDS_BUILTIN_TRANSPORTS="${FASTDDS_BUILTIN_TRANSPORTS:-UDPv4}"

export CYCLONEDDS_URI_REAL=$SCRIPT_DIR/cyclonedds_real.xml
export CYCLONEDDS_URI_REMOTE=$SCRIPT_DIR/cyclonedds_remote.xml
