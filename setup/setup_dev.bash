export CLI_CONFIGURATION="dev"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
export WORKSPACE_DIR

if [ -f /opt/ros/jazzy/setup.bash ]; then
    source /opt/ros/jazzy/setup.bash
fi

if [ -f "$WORKSPACE_DIR/install/setup.bash" ]; then
    source "$WORKSPACE_DIR/install/setup.bash"
fi

source "$SCRIPT_DIR/cli_path.bash"
source "$SCRIPT_DIR/paths.bash"

export SIMULATION="true"
export III_SYSTEM_PROFILE="sim"
export III_ENVIRONMENT_PROFILE="dev"
export III_DEFAULT_TARGET="sim"

# Gazebo Transport discovers peers over UDP multicast by default. For the local
# simulation stack all Gazebo transport peers run on the same host, so bind to
# loopback by default. This avoids Gazebo selecting transient external or down
# Docker bridge interfaces and wedging simulation-side bridge nodes. Operators
# can still set GZ_IP explicitly before sourcing this profile.
if [ -z "${GZ_IP:-}" ]; then
    export GZ_IP="127.0.0.1"
fi

III_DRONE_SIM_PLUGIN_DIR="$WORKSPACE_DIR/install/iii_drone_simulation/lib"
if [ -d "$III_DRONE_SIM_PLUGIN_DIR" ]; then
    case ":${GZ_SIM_SYSTEM_PLUGIN_PATH:-}:" in
        *":$III_DRONE_SIM_PLUGIN_DIR:"*)
            ;;
        *)
            export GZ_SIM_SYSTEM_PLUGIN_PATH="$III_DRONE_SIM_PLUGIN_DIR${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
            ;;
    esac
fi
unset III_DRONE_SIM_PLUGIN_DIR

export COLCON_HOME="$WORKSPACE_DIR"

source "$SCRIPT_DIR/node_log_levels.bash"
source "$SCRIPT_DIR/ros_setup.bash"

# Prevent stale GTest paths (e.g. /opt/ros/humble/src/gtest_vendor) from
# overriding ament_cmake_gtest resolution in Jazzy builds.
unset GTEST_DIR
unset GTEST_ROOT
unset GTEST_INCLUDE_DIRS
unset GTEST_LIBRARIES
unset GTEST_MAIN_LIBRARIES
unset GMOCK_LIBRARIES

# Remove leaked Humble underlay prefixes from development shells.
_strip_humble_prefixes() {
    local var_name="$1"
    local value="${!var_name}"
    local old_ifs="$IFS"
    local cleaned=""
    local token
    IFS=':'
    for token in $value; do
        case "$token" in
            *"/opt/ros/humble"*)
                ;;
            *)
                if [ -z "$cleaned" ]; then
                    cleaned="$token"
                else
                    cleaned="${cleaned}:$token"
                fi
                ;;
        esac
    done
    IFS="$old_ifs"
    export "$var_name=$cleaned"
}

_strip_humble_prefixes AMENT_PREFIX_PATH
_strip_humble_prefixes CMAKE_PREFIX_PATH
_strip_humble_prefixes COLCON_PREFIX_PATH
_strip_humble_prefixes PYTHONPATH
unset -f _strip_humble_prefixes

export CYCLONEDDS_URI=

export ROS_LOG_DIR_BASE=$WORKSPACE_DIR/runtime_logs

DEBUGGABLE_NODES="$(tr '\n' ' ' < "$SCRIPT_DIR/debuggable_nodes.txt")"
export DEBUGGABLE_NODES

source "$SCRIPT_DIR/python_debug_ports.bash"
