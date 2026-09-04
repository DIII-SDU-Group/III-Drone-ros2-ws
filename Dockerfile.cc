# syntax=docker/dockerfile:1.7

# Keep these values byte-for-byte aligned with deployment/targets/v1/
# raspberry-pi-5-noble-arm64.json. The index digest makes the input stable;
# Docker resolves the requested platform from that immutable index.
ARG TARGET_IMAGE=docker.io/library/ros:jazzy-perception@sha256:63407fb78383d0c68849c2913a3b6a5675069d2c2c33c21b3e7c454e028e8b5d
ARG BUILDER_IMAGE=docker.io/library/ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517
ARG BUILD_TOOLS_IMAGE=docker.io/library/ros:jazzy-ros-base@sha256:2589a8fba5257307857890173c069852c2abf913a0be7970f172478baecb09e4

FROM --platform=linux/arm64 ${TARGET_IMAGE} AS target-seed
ARG UBUNTU_SNAPSHOT=https://snapshot.ubuntu.com/ubuntu/20260801T000000Z
ARG ROS_SNAPSHOT=http://snapshots.ros.org/jazzy/2026-06-18/ubuntu
ARG ROS_SNAPSHOT_KEY_SHA256=6d2ff4af9d56b304213de7664551f6986174a68bae76476b7ad21469b27a28c4
ARG GENERATE_PARAMETER_LIBRARY_VERSION=0.7.3-1noble.20260612.124157
ARG GENERATE_PARAMETER_LIBRARY_PY_VERSION=0.7.3-1noble.20260514.121849
ARG PARAMETER_TRAITS_VERSION=0.7.3-1noble.20260612.123910
ARG USB_CAM_VERSION=0.8.1-1noble.20260614.090116
ARG RMW_CYCLONEDDS_VERSION=2.2.3-1noble.20260612.091852

# Extend the immutable ROS perception seed only from date-addressed, signed
# Ubuntu and ROS repositories. The snapshot-builder key is checked both by
# file hash and primary fingerprint before apt is allowed to consume it.
COPY deployment/keys/ros-snapshot-builder.asc /tmp/ros-snapshot-builder.asc
RUN echo "${ROS_SNAPSHOT_KEY_SHA256}  /tmp/ros-snapshot-builder.asc" | sha256sum -c - && \
    gpg --show-keys --with-colons /tmp/ros-snapshot-builder.asc | \
      grep -Fqx 'fpr:::::::::4B63CF8FDE49746E98FA01DDAD19BAB3CBF125EA:' && \
    gpg --dearmor --batch --yes \
      --output /usr/share/keyrings/ros-snapshot-builder.gpg \
      /tmp/ros-snapshot-builder.asc && \
    printf '%s\n' \
      'Types: deb' \
      "URIs: ${UBUNTU_SNAPSHOT}" \
      'Suites: noble noble-updates noble-security' \
      'Components: main universe' \
      'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/ubuntu.sources && \
    printf '%s\n' \
      'Types: deb' \
      "URIs: ${ROS_SNAPSHOT}" \
      'Suites: noble' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/ros-snapshot-builder.gpg' \
      > /etc/apt/sources.list.d/ros2.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      ros-jazzy-generate-parameter-library=${GENERATE_PARAMETER_LIBRARY_VERSION} \
      ros-jazzy-generate-parameter-library-py=${GENERATE_PARAMETER_LIBRARY_PY_VERSION} \
      ros-jazzy-parameter-traits=${PARAMETER_TRAITS_VERSION} \
      ros-jazzy-usb-cam=${USB_CAM_VERSION} \
      ros-jazzy-rmw-cyclonedds-cpp=${RMW_CYCLONEDDS_VERSION} && \
    rm -f /tmp/ros-snapshot-builder.asc && \
    rm -rf /var/lib/apt/lists/*

FROM target-seed AS target-sysroot

FROM --platform=linux/amd64 ${BUILD_TOOLS_IMAGE} AS ros-build-tools

FROM --platform=linux/amd64 ${BUILDER_IMAGE} AS toolchain
ARG UBUNTU_SNAPSHOT=https://snapshot.ubuntu.com/ubuntu/20260801T000000Z
ARG CA_CERTIFICATES_VERSION=20260601~24.04.1
ARG GCC_VERSION=13.3.0-6ubuntu2~24.04.1cross1
ARG LIBC_DEV_VERSION=2.39-0ubuntu8cross1
ARG CMAKE_VERSION=3.28.3-1build7
ARG NINJA_VERSION=1.11.1-2
ARG MAKE_VERSION=4.3-4.1build2
ARG GIT_VERSION=1:2.43.0-1ubuntu7.3
ARG PYTHON_VERSION=3.12.3-0ubuntu2.1
ARG CCACHE_VERSION=4.9.1-1
ARG QEMU_USER_STATIC_VERSION=1:8.2.2+ds-0ubuntu1.17

# Bootstrap CA certificates from a signed snapshot index, then require normal
# TLS and apt signature verification for every remaining package.
RUN printf '%s\n' \
      "Types: deb" \
      "URIs: ${UBUNTU_SNAPSHOT}" \
      "Suites: noble noble-updates noble-security" \
      "Components: main universe" \
      "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" \
      > /etc/apt/sources.list.d/ubuntu.sources && \
    apt-get -o Acquire::https::Verify-Peer=false update && \
    apt-get -o Acquire::https::Verify-Peer=false install -y --no-install-recommends \
      ca-certificates=${CA_CERTIFICATES_VERSION} && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      gcc-13-aarch64-linux-gnu=${GCC_VERSION} \
      g++-13-aarch64-linux-gnu=${GCC_VERSION} \
      libc6-dev-arm64-cross=${LIBC_DEV_VERSION} \
      cmake=${CMAKE_VERSION} \
      ninja-build=${NINJA_VERSION} \
      make=${MAKE_VERSION} \
      git=${GIT_VERSION} \
      python3=${PYTHON_VERSION} \
      ccache=${CCACHE_VERSION} \
      qemu-user-static=${QEMU_USER_STATIC_VERSION} && \
    rm -rf /var/lib/apt/lists/*

# The sysroot is generated from the immutable ARM64 target seed. It is never
# copied from an aircraft and contains no mutable aircraft state.
COPY --from=target-sysroot / /opt/iii/sysroot
# Colcon and its ROS build extensions are pure Python build tools from the
# pinned amd64 ROS image. Target libraries still come only from target-sysroot.
COPY --from=ros-build-tools /usr/bin/colcon /usr/bin/colcon
COPY --from=ros-build-tools /usr/lib/python3/dist-packages/ /opt/iii/ros-build-tools/
COPY --from=ros-build-tools /opt/ros/jazzy/lib/python3.12/site-packages/ /opt/iii/ros-build-tools/ros/
COPY --from=ros-build-tools /usr/lib/x86_64-linux-gnu/blas/libblas.so.3.12.0 /opt/iii/ros-build-libs/libblas.so.3
COPY --from=ros-build-tools /usr/lib/x86_64-linux-gnu/lapack/liblapack.so.3.12.0 /opt/iii/ros-build-libs/liblapack.so.3
COPY --from=ros-build-tools /usr/lib/x86_64-linux-gnu/libgfortran.so.5.0.0 /opt/iii/ros-build-libs/libgfortran.so.5
COPY cc_ws/arm64-toolchain.cmake /opt/iii/arm64-toolchain.cmake
COPY cc_ws/run-target-emulated.sh /usr/local/bin/iii-run-target-emulated
COPY entrypoint_cc.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh /usr/local/bin/iii-run-target-emulated && mkdir -p /home/iii/ws
WORKDIR /home/iii/ws
ENV III_TARGET_ID=raspberry-pi-5-noble-arm64 \
    III_SYSTEM_PROFILE=real \
    III_SYSROOT=/opt/iii/sysroot \
    ROS_DISTRO=jazzy \
    CMAKE_TOOLCHAIN_FILE=/opt/iii/arm64-toolchain.cmake \
    CCACHE_DIR=/cache/ccache \
    CCACHE_BASEDIR=/home/iii/ws \
    CCACHE_NOHASHDIR=true \
    CCACHE_COMPILERCHECK=content \
    CCACHE_MAXSIZE=10G \
    PYTHONPATH=/opt/iii/ros-build-tools:/opt/iii/ros-build-tools/ros:/opt/iii/sysroot/opt/ros/jazzy/lib/python3.12/site-packages:/opt/iii/sysroot/usr/lib/python3/dist-packages \
    LD_LIBRARY_PATH=/opt/iii/ros-build-libs

COPY deployment/targets/probe/abi_probe.c /tmp/abi_probe.c
RUN /usr/bin/aarch64-linux-gnu-gcc-13 -O2 -Wall -Wextra -Werror \
      /tmp/abi_probe.c -o /tmp/iii-target-abi-probe && \
    rm /tmp/abi_probe.c

FROM target-sysroot AS target-runtime
ENV III_TARGET_ID=raspberry-pi-5-noble-arm64 \
    III_SYSTEM_PROFILE=real \
    ROS_DISTRO=jazzy

FROM target-runtime AS abi-probe
ARG TARGET_PLATFORM_DIGEST=sha256:cf36a2ca2ce9d3f239ab3df02430d580a7d643d907cd7bf0926b6f18fb7bd769
COPY --from=toolchain /tmp/iii-target-abi-probe /usr/local/bin/iii-target-abi-probe
COPY deployment/targets/probe/runtime_probe.py /usr/local/bin/iii-target-runtime-probe
ENV III_TARGET_IMAGE_PLATFORM_DIGEST=${TARGET_PLATFORM_DIGEST}
ENTRYPOINT ["/usr/bin/python3", "/usr/local/bin/iii-target-runtime-probe"]

FROM toolchain AS cross-compiler
ENTRYPOINT ["/entrypoint.sh"]
