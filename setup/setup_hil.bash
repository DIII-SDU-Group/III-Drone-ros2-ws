#!/usr/bin/env bash
# Split-host HIL operator profile: runtime on iii.local, Gazebo/PX4 SITL on the
# workstation. The aircraft Runtime API remains the only III control boundary.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# A command-selected endpoint is normalized by the launcher/coordinator. When
# this profile is sourced directly, an explicit HIL endpoint (or legacy IPv4
# override) wins over inherited remote-runtime aliases.
III_HIL_TARGET_SELECTED="${III_HIL_PI_ENDPOINT:-${III_HIL_PI_ADDRESS:-${III_HIL_HOST_PI_ADDRESS:-}}}"
III_HIL_API_URL_HOST=""
if [[ -n "${III_RUNTIME_API_URL:-}" && "${III_RUNTIME_API_URL}" =~ ^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:]+) ]]; then
    III_HIL_API_URL_HOST="${BASH_REMATCH[1]}"
fi
III_HIL_TARGET="${III_HIL_TARGET_SELECTED:-${III_RUNTIME_API_HOST:-${III_RUNTIME_HOST:-${III_SSH_HOST:-${III_HIL_API_URL_HOST:-iii.local}}}}}"
unset III_HIL_RESOLVED_PI_ADDRESS
export III_HIL_PI_ENDPOINT="${III_HIL_TARGET}"
export III_HIL_PI_ADDRESS="${III_HIL_PI_ADDRESS:-}"
export III_RUNTIME_HOST="${III_HIL_TARGET}"
export III_RUNTIME_API_HOST="${III_HIL_TARGET}"
export III_SSH_HOST="${III_HIL_TARGET}"
if [[ -n "${III_RUNTIME_API_URL:-}" && -z "${III_HIL_TARGET_SELECTED}" ]]; then
    : # Preserve a standalone explicit API URL when no HIL target was selected.
else
    export III_RUNTIME_API_URL="http://${III_HIL_TARGET}:8765"
fi
source "$SCRIPT_DIR/setup_field.bash"

# Onboard, the systemd daemon uses the deployed runtime paths rather than the
# editable workspace defaults from setup/paths.bash. Import only local paths:
# /etc/iii/runtime.env also contains listener settings such as
# III_RUNTIME_API_HOST=0.0.0.0, which must not replace the selected Pi target.
III_HIL_ONBOARD_RUNTIME_ENV="${III_HIL_ONBOARD_RUNTIME_ENV:-/etc/iii/runtime.env}"
hil_onboard_ros_domain_id=""
if [[ -r "${III_HIL_ONBOARD_RUNTIME_ENV}" ]] &&
   grep -qx 'III_SYSTEM_PROFILE=hil' "${III_HIL_ONBOARD_RUNTIME_ENV}"; then
    while IFS='=' read -r key value; do
        case "${key}" in
            III_SYSTEM_RUNTIME_DIR|III_SYSTEM_DAEMON_SOCKET|III_SYSTEM_DAEMON_LOG|CONFIG_BASE_DIR)
                [[ "${value}" == /* ]] || continue
                export "${key}=${value}"
                ;;
            ROS_DOMAIN_ID)
                # The provisioned stack domain (iii_ros_domain_id).
                [[ "${value}" =~ ^[0-9]+$ ]] || continue
                hil_onboard_ros_domain_id="${value}"
                ;;
        esac
    done <"${III_HIL_ONBOARD_RUNTIME_ENV}"
fi

export III_SYSTEM_PROFILE="hil"
export III_ENVIRONMENT_PROFILE="hil"
export III_RUNTIME_HOST_PROFILE="hil"
export SIMULATION="true"
export III_DEFAULT_TARGET="hil"
export III_RUNTIME_TARGET="hil"
if [[ -r "${III_HIL_ONBOARD_RUNTIME_ENV:-/etc/iii/runtime.env}" ]] &&
   grep -qx 'III_SYSTEM_PROFILE=hil' "${III_HIL_ONBOARD_RUNTIME_ENV:-/etc/iii/runtime.env}"; then
    # Onboard CLI commands use the local daemon. Workstation commands retain
    # their legacy HTTP behavior unless a native GC install routes them over SSH.
    export CLI_CONFIGURATION="dev"
else
    export CLI_CONFIGURATION="remote"
fi
unset III_HIL_ONBOARD_RUNTIME_ENV
export III_PX4_TARGET_SYSTEM="${III_PX4_TARGET_SYSTEM:-8}"
export III_PX4_TARGET_COMPONENT="${III_PX4_TARGET_COMPONENT:-1}"
# The workstation HIL launcher terminates the virtual PX4 MAVLink link on the
# Pi here.  Keep this explicit so MCP tools never attach to the local-sim
# default (14540) or the physical PX4 transport by accident.
export III_PX4_SYSTEM_ADDRESS="${III_PX4_SYSTEM_ADDRESS:-udpin://0.0.0.0:14544}"

# HIL tools launched directly on the Pi and workstation must use the same
# middleware as the Pi's PX4 XRCE agent. Mixed Fast DDS/Cyclone DDS discovery
# across the split-host link can leave BatteryStatus invisible at the payload.
# Onboard, the provisioned domain is the default; the workstation keeps 42
# unless III_HIL_ROS_DOMAIN_ID selects the Pi's provisioned domain.
export ROS_DOMAIN_ID="${III_HIL_ROS_DOMAIN_ID:-${hil_onboard_ros_domain_id:-42}}"
unset hil_onboard_ros_domain_id
export RMW_IMPLEMENTATION="rmw_fastrtps_cpp"
export FASTDDS_BUILTIN_TRANSPORTS="UDPv4"
unset CYCLONEDDS_URI
# Keep operator and GC endpoints pinned to the same reachable Pi address even
# though Fast DDS no longer needs a Cyclone peer URI. The selected source route
# is also used by the workstation PX4/Gazebo launcher.
# Resolution diagnostics go to stderr (stdout carries only "peer source").
# A failed resolution is reported but not fatal here: the launcher and the
# coordinator re-check reachability and refuse to proceed on their own.
if [[ "${CLI_CONFIGURATION}" == "remote" && -z "${III_HIL_PI_ADDRESS}" ]]; then
    if hil_selected_route="$(python3 "${SCRIPT_DIR}/../scripts/workspace/resolve_hil_peer.py" \
        "${III_HIL_PI_ENDPOINT}" "${III_HIL_PEER_STATE_DIR:-${WORKSPACE_DIR}/runtime}" \
        "${III_HIL_PX4_INSTANCE:-0}")" && [[ -n "${hil_selected_route}" ]]; then
        read -r hil_peer_address hil_source_address <<<"${hil_selected_route}"
        export III_HIL_RESOLVED_PI_ADDRESS="${hil_peer_address}"
        export III_HIL_WORKSTATION_ADDRESS="${III_HIL_WORKSTATION_ADDRESS:-${hil_source_address}}"
    else
        echo "HIL setup: no reachable Pi IPv4 was resolved for ${III_HIL_PI_ENDPOINT}; continuing with the hostname." >&2
    fi
    unset hil_selected_route hil_peer_address hil_source_address
fi
