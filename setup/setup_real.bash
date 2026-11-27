#!/usr/bin/env bash
# Onboard real-aircraft runtime profile; setup_opti_track.bash sources it too.
#
# On a provisioned Pi, /etc/iii/runtime.env is the runtime contract that the
# system daemon and Runtime API read. This shell imports the same daemon
# socket, daemon log, runtime directory, configuration root, and ROS 2/DDS
# settings from it (the stack domain is the one PX4's UXRCE_DDS_DOM_ID must
# equal). Elsewhere (workstation, devcontainer, container image) the editable
# workspace defaults from setup/paths.bash apply. Sourcing it again is safe.

_iii_setup_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
III_ROS_PREFIX="${III_ROS_PREFIX:-/opt/ros/jazzy}"

if [[ ! -r "${III_ROS_PREFIX}/setup.bash" ]]; then
  echo "III real profile requires ROS Jazzy at ${III_ROS_PREFIX}" >&2
  unset _iii_setup_dir
  return 30 2>/dev/null || exit 30
fi
source "${III_ROS_PREFIX}/setup.bash"

# The editable workspace install is the aircraft runtime: the systemd units
# source the same tree. III_WORKSPACE_INSTALL selects another colcon install
# (for example the container entrypoint's).
_iii_workspace_install="${III_WORKSPACE_INSTALL:-$(dirname "${_iii_setup_dir}")/install}"
if [[ -r "${_iii_workspace_install}/setup.bash" ]]; then
  source "${_iii_workspace_install}/setup.bash"
fi

source "${_iii_setup_dir}/cli_path.bash"
source "${_iii_setup_dir}/paths.bash"
export WORKSPACE_DIR
export CLI_CONFIGURATION="dev"
export SIMULATION="false"
export III_SYSTEM_PROFILE="real"
export III_ENVIRONMENT_PROFILE="real"
export III_DEFAULT_TARGET="real"
export III_RUNTIME_TARGET="real"
source "${_iii_setup_dir}/node_log_levels.bash"
source "${_iii_setup_dir}/ros_setup.bash"

# Off the Pi, use the provisioning defaults for the aircraft stack's DDS
# (deployment/ansible/roles/runtime_control_plane/templates/runtime.env.j2).
# The runtime is Fast DDS only; a Cyclone DDS URI is never meaningful here.
export ROS_DOMAIN_ID="${III_ROS_DOMAIN_ID:-42}"
export RMW_IMPLEMENTATION="rmw_fastrtps_cpp"
export FASTDDS_BUILTIN_TRANSPORTS="UDPv4"
unset CYCLONEDDS_URI

# Onboard, the provisioned runtime environment wins. Import only local paths
# and DDS settings: /etc/iii/runtime.env also holds listener settings such as
# III_RUNTIME_API_HOST=0.0.0.0, which are not client endpoints.
_iii_runtime_env="${III_ONBOARD_RUNTIME_ENV:-/etc/iii/runtime.env}"
if [[ -r "${_iii_runtime_env}" ]] &&
   grep -Eqx 'III_SYSTEM_PROFILE=(hil|real|opti_track)' "${_iii_runtime_env}"; then
  while IFS='=' read -r _iii_key _iii_value; do
    case "${_iii_key}" in
      III_SYSTEM_RUNTIME_DIR|III_SYSTEM_DAEMON_SOCKET|III_SYSTEM_DAEMON_LOG|CONFIG_BASE_DIR)
        if [[ "${_iii_value}" == /* ]]; then
          export "${_iii_key}=${_iii_value}"
        fi
        ;;
      ROS_DOMAIN_ID)
        if [[ "${_iii_value}" =~ ^[0-9]+$ ]]; then
          export "${_iii_key}=${_iii_value}"
        fi
        ;;
      ROS_LOCALHOST_ONLY|ROS_AUTOMATIC_DISCOVERY_RANGE|RMW_IMPLEMENTATION|FASTDDS_BUILTIN_TRANSPORTS)
        if [[ "${_iii_value}" =~ ^[A-Za-z0-9_]+$ ]]; then
          export "${_iii_key}=${_iii_value}"
        fi
        ;;
    esac
  done <"${_iii_runtime_env}"
fi
unset _iii_setup_dir _iii_workspace_install _iii_runtime_env _iii_key _iii_value
