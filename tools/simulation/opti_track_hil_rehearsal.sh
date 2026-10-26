#!/usr/bin/env bash
# OptiTrack rehearsal on HIL: the Pi runs the `opti_track` runtime profile
# against the workstation's PX4 SITL.
#
#   - Workstation: the HIL simulation (launch_hil_workstation.sh) with a
#     vision-only PX4 estimator, plus the simulated lab gateway, which
#     publishes the rigid-body pose in lab ROS domain 0 only.
#   - Pi: a removable systemd drop-in switches the daemon and Runtime API to
#     profile opti_track while keeping the HIL transport (agent port, MAVLink
#     endpoint, stack domain). The relay subscribes the pose in domain 0 over
#     the Pi-workstation link, as it does over the lab Wi-Fi. III_PX4_SIMULATED
#     tells the runtime that the PX4 is not the flight controller, so its
#     transport parameters are not held to the opti_track PX4 baseline.
#
# The physical flight controller is not involved and must stay disarmed.
# `stop` restores the Pi to its provisioned HIL profile.
#
# Usage (on the workstation, from the workspace root):
#   tools/simulation/opti_track_hil_rehearsal.sh start|stop|status --host <pi>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
ACTION="${1:-}"
[[ "${2:-}" == "--host" && -n "${3:-}" ]] || { echo "usage: $0 start|stop|status --host <pi>" >&2; exit 64; }
PI="$3"
RIGID_BODY_ID="${III_OPTI_TRACK_REHEARSAL_RIGID_BODY_ID:-1}"
GATEWAY_SESSION="iii_opti_track_gateway"
LAUNCHER="${ROOT}/tools/simulation/launch_hil_workstation.sh"
DROP_IN="60-opti-track-rehearsal.conf"
PI_ENV="/etc/iii/opti-track-rehearsal.env"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "iii@${PI}")
# deployment/px4/parameters/opti_track.params, without its transport and battery lines.
PX4_VISION_ENV="PX4_PARAM_EKF2_EV_CTRL=11 PX4_PARAM_EKF2_HGT_REF=3 PX4_PARAM_EKF2_GPS_CTRL=0 PX4_PARAM_EKF2_BARO_CTRL=1 PX4_PARAM_EKF2_MAG_TYPE=5 PX4_PARAM_SYS_HAS_MAG=0 PX4_PARAM_SYS_HAS_GPS=0 PX4_PARAM_SENS_EN_GPSSIM=0 PX4_PARAM_SENS_EN_MAGSIM=0 PX4_PARAM_EKF2_EV_NOISE_MD=0 PX4_PARAM_EKF2_EVP_NOISE=0.05 PX4_PARAM_EKF2_EVA_NOISE=0.05 PX4_PARAM_EKF2_EV_QMIN=0 PX4_PARAM_EKF2_EV_DELAY=30 PX4_PARAM_EKF2_NOAID_TOUT=1000000 PX4_PARAM_COM_POSCTL_NAVL=0 PX4_PARAM_COM_POS_FS_EPH=1.0 PX4_PARAM_MIS_TAKEOFF_ALT=1.2"

container() {
    docker ps --filter "label=devcontainer.local_folder=${ROOT}" --format '{{.ID}}' | head -n1
}

pi_runtime() {
    # Run a command on the Pi in the opti_track operator shell, which takes
    # the runtime paths and DDS settings from /etc/iii/runtime.env.
    "${SSH[@]}" "source /home/iii/ws/setup/setup_opti_track.bash; $1"
}

pi_profile_opti_track() {
    "${SSH[@]}" "sudo tee ${PI_ENV} >/dev/null <<'ENV'
III_SYSTEM_PROFILE=opti_track
III_RUNTIME_API_PROFILE=opti_track
SIMULATION=false
III_MICRO_ROS_AGENT_UDP_PORT=8890
III_PX4_SIMULATED=1
ENV
for unit in iii-system-daemon iii-runtime-api; do
  sudo mkdir -p /etc/systemd/system/\${unit}.service.d
  printf '[Service]\nEnvironmentFile=${PI_ENV}\n' | sudo tee /etc/systemd/system/\${unit}.service.d/${DROP_IN} >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl restart iii-system-daemon.service iii-runtime-api.service"
}

pi_profile_restore() {
    "${SSH[@]}" "for unit in iii-system-daemon iii-runtime-api; do
  sudo rm -f /etc/systemd/system/\${unit}.service.d/${DROP_IN}
done
sudo rm -f ${PI_ENV}
sudo systemctl daemon-reload
sudo systemctl restart iii-system-daemon.service iii-runtime-api.service"
}

pi_shutdown_runtime() {
    pi_runtime "iii system shutdown --confirm --non-interactive" >/dev/null 2>&1 || true
}

rehearsal_parameters() {
    pi_runtime "python3 - $*" <"${ROOT}/tools/simulation/opti_track_rehearsal_parameters.py"
}

start_gateway() {
    local id
    id="$(container)"
    [[ -n "${id}" ]] || { echo "workspace devcontainer is not running" >&2; return 1; }
    # The gateway's own node joins the HIL adapters' domain; only its pose
    # publisher is in lab domain 0.
    docker exec -u iii "${id}" bash -lc "tmux kill-session -t ${GATEWAY_SESSION} 2>/dev/null; \
tmux new-session -d -s ${GATEWAY_SESSION} -n gateway \"bash -lc 'source /home/iii/ws/setup/setup_hil.bash && \
exec ros2 run iii_drone_simulation simulated_lab_mocap_gateway --ros-args \
-p rigid_body_id:=${RIGID_BODY_ID} -p use_sim_time:=true -p lab_domain_id:=0'\""
}

stop_gateway() {
    local id
    id="$(container)"
    [[ -z "${id}" ]] || docker exec -u iii "${id}" bash -lc "tmux kill-session -t ${GATEWAY_SESSION} 2>/dev/null" || true
}

case "${ACTION}" in
    start)
        pi_shutdown_runtime
        "${LAUNCHER}" stop --host "${PI}" >/dev/null 2>&1 || true
        pi_profile_opti_track
        rehearsal_parameters prepare "${RIGID_BODY_ID}"
        III_HIL_PX4_EXTRA_ENV="${PX4_VISION_ENV}" "${LAUNCHER}" start --host "${PI}" --headless
        start_gateway
        pi_runtime "iii system boot --profile opti_track --confirm --non-interactive && \
iii system start --confirm --non-interactive"
        ;;
    stop)
        pi_shutdown_runtime
        stop_gateway
        "${LAUNCHER}" stop --host "${PI}" || true
        rehearsal_parameters restore
        pi_profile_restore
        ;;
    status)
        "${LAUNCHER}" status --host "${PI}" || true
        pi_runtime "iii system status" || true
        ;;
    *)
        echo "usage: $0 start|stop|status --host <pi>" >&2
        exit 64
        ;;
esac
