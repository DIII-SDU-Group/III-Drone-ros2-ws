#!/usr/bin/env bash
# OptiTrack rehearsal environment in the SIM devcontainer.
#
# Runs the `opti_track` runtime profile against PX4 SITL and Gazebo:
#   - PX4 SITL with a vision-only estimator (the lab's PX4 baseline, see
#     deployment/px4/opti-track.nsh): no GPS, no magnetometer;
#   - the Gazebo bridges for the clock and the ground-truth odometry;
#   - the simulated lab gateway, which republishes the ground truth as the
#     lab's rigid-body pose topic;
#   - the Runtime API and the system daemon in profile opti_track.
# Everything shares ROS domain 0, as the SIM stack does.
#
# Usage (inside the devcontainer): opti_track_rehearsal.sh start|stop|status
set -euo pipefail

WORKSPACE_ROOT="${III_SIM_TOOLS_WORKSPACE_ROOT:-/home/iii/ws}"
SIM_TOOLS="${WORKSPACE_ROOT}/tools/simulation/launch_simulation_tools.sh"
ADAPTER_SESSION="iii_opti_track_rehearsal"
RIGID_BODY_ID="${III_OPTI_TRACK_REHEARSAL_RIGID_BODY_ID:-1}"
API_ENV_FILE="${WORKSPACE_ROOT}/.config/iii-runtime-api.env"
API_ENV_BACKUP="${API_ENV_FILE}.before-opti-track-rehearsal"
# deployment/px4/opti-track.nsh, without its transport and battery lines.
PX4_VISION_ENV="PX4_PARAM_EKF2_EV_CTRL=11 PX4_PARAM_EKF2_HGT_REF=3 PX4_PARAM_EKF2_GPS_CTRL=0 PX4_PARAM_EKF2_BARO_CTRL=1 PX4_PARAM_EKF2_MAG_TYPE=5 PX4_PARAM_SYS_HAS_MAG=0 PX4_PARAM_SYS_HAS_GPS=0 PX4_PARAM_SENS_EN_GPSSIM=0 PX4_PARAM_SENS_EN_MAGSIM=0 PX4_PARAM_EKF2_EV_NOISE_MD=0 PX4_PARAM_EKF2_EVP_NOISE=0.05 PX4_PARAM_EKF2_EVA_NOISE=0.05 PX4_PARAM_EKF2_EV_QMIN=0 PX4_PARAM_EKF2_EV_DELAY=30 PX4_PARAM_EKF2_NOAID_TOUT=1000000 PX4_PARAM_COM_POSCTL_NAVL=0 PX4_PARAM_COM_POS_FS_EPH=1.0 PX4_PARAM_MIS_TAKEOFF_ALT=1.2"

# The ROS setup files read unset variables.
set +u
# shellcheck disable=SC1091
source "${WORKSPACE_ROOT}/setup/setup_dev.bash"
set -u

REHEARSAL_PARAMETERS="${WORKSPACE_ROOT}/tools/simulation/opti_track_rehearsal_parameters.py"

ensure_clock_tracking() {
    # The opti_track runtime gates on a settled chrony clock, as on the
    # aircraft. In the container chronyd only tracks (-x): it cannot and need
    # not steer the host clock.
    command -v chronyc >/dev/null || {
        echo "chrony is required for the opti_track clock gate: sudo apt-get install chrony" >&2
        return 1
    }
    pgrep -x chronyd >/dev/null || sudo chronyd -x
}

start() {
    ensure_clock_tracking
    iii system shutdown --confirm --non-interactive >/dev/null 2>&1 || true
    III_SIM_TOOLS_PX4_EXTRA_ENV="${PX4_VISION_ENV}" "${SIM_TOOLS}" --recreate --no-attach --headless

    tmux kill-session -t "${ADAPTER_SESSION}" 2>/dev/null || true
    local setup="source '${WORKSPACE_ROOT}/setup/setup_dev.bash'"
    tmux new-session -d -s "${ADAPTER_SESSION}" -n bridges \
        "bash -lc $(printf '%q' "${setup} && exec ros2 launch iii_drone_simulation sim_assets.launch.py include_diagnostics:=false")"
    tmux new-window -t "${ADAPTER_SESSION}" -n gateway \
        "bash -lc $(printf '%q' "${setup} && exec ros2 run iii_drone_simulation simulated_lab_mocap_gateway --ros-args -p rigid_body_id:=${RIGID_BODY_ID} -p use_sim_time:=true")"

    # Keep the operator's own override, but never a rehearsal's leftover.
    if [[ -f "${API_ENV_FILE}" && ! -f "${API_ENV_BACKUP}" ]] &&
        ! grep -qx 'III_RUNTIME_API_PROFILE=opti_track' "${API_ENV_FILE}"; then
        cp -p "${API_ENV_FILE}" "${API_ENV_BACKUP}"
    fi
    mkdir -p "$(dirname "${API_ENV_FILE}")"
    # The PX4 is SITL, not the flight controller the opti_track baseline is for.
    printf 'III_RUNTIME_API_PROFILE=opti_track\nIII_PX4_SIMULATED=1\n' >"${API_ENV_FILE}"
    sudo systemctl restart iii-runtime-api.service

    python3 "${REHEARSAL_PARAMETERS}" prepare "${RIGID_BODY_ID}"
    iii system boot --profile opti_track --confirm --non-interactive
    iii system start --confirm --non-interactive
}

stop() {
    iii system shutdown --confirm --non-interactive >/dev/null 2>&1 || true
    tmux kill-session -t "${ADAPTER_SESSION}" 2>/dev/null || true
    "${SIM_TOOLS}" --stop || true
    python3 "${REHEARSAL_PARAMETERS}" restore
    if [[ -f "${API_ENV_BACKUP}" ]]; then
        mv -f "${API_ENV_BACKUP}" "${API_ENV_FILE}"
    else
        rm -f "${API_ENV_FILE}"
    fi
    sudo systemctl restart iii-runtime-api.service
}

status() {
    "${SIM_TOOLS}" --status || true
    tmux list-windows -t "${ADAPTER_SESSION}" 2>/dev/null || echo "adapters: not running"
    iii system status || true
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) echo "usage: $0 start|stop|status" >&2; exit 64 ;;
esac
