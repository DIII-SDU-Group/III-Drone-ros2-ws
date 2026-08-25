# syntax=docker/dockerfile:1.7

# Keep these values byte-for-byte aligned with deployment/targets/v1/
# raspberry-pi-5-noble-arm64.json. The index digest makes the input stable;
# Docker resolves the requested platform from that immutable index.
ARG TARGET_IMAGE=docker.io/library/ros:jazzy-ros-base@sha256:2589a8fba5257307857890173c069852c2abf913a0be7970f172478baecb09e4
ARG BUILDER_IMAGE=docker.io/library/ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

FROM --platform=linux/arm64 ${TARGET_IMAGE} AS target-sysroot

FROM --platform=linux/amd64 ${BUILDER_IMAGE} AS toolchain
ARG UBUNTU_SNAPSHOT=https://snapshot.ubuntu.com/ubuntu/20260810T000000Z
ARG CA_CERTIFICATES_VERSION=20260601~24.04.1
ARG GCC_VERSION=13.3.0-6ubuntu2~24.04.1cross1
ARG LIBC_DEV_VERSION=2.39-0ubuntu8cross1
ARG CMAKE_VERSION=3.28.3-1build7
ARG NINJA_VERSION=1.11.1-2
ARG MAKE_VERSION=4.3-4.1build2
ARG GIT_VERSION=1:2.43.0-1ubuntu7.3
ARG PYTHON_VERSION=3.12.3-0ubuntu2.1

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
      python3=${PYTHON_VERSION} && \
    rm -rf /var/lib/apt/lists/*

# The sysroot is generated from the immutable ARM64 target seed. It is never
# copied from an aircraft and contains no mutable aircraft state.
COPY --from=target-sysroot / /opt/iii/sysroot
COPY cc_ws/arm64-toolchain.cmake /opt/iii/arm64-toolchain.cmake
COPY entrypoint_cc.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh && mkdir -p /home/iii/ws
WORKDIR /home/iii/ws
ENV III_TARGET_ID=raspberry-pi-5-noble-arm64 \
    III_SYSROOT=/opt/iii/sysroot \
    ROS_DISTRO=jazzy \
    CMAKE_TOOLCHAIN_FILE=/opt/iii/arm64-toolchain.cmake

COPY deployment/targets/probe/abi_probe.c /tmp/abi_probe.c
RUN /usr/bin/aarch64-linux-gnu-gcc-13 -O2 -Wall -Wextra -Werror \
      /tmp/abi_probe.c -o /tmp/iii-target-abi-probe && \
    rm /tmp/abi_probe.c

FROM target-sysroot AS abi-probe
ARG TARGET_PLATFORM_DIGEST=sha256:d849b6203853848bf20f5e5d6d77c1275bff1ff727d93ab055799cb33c2dac7a
COPY --from=toolchain /tmp/iii-target-abi-probe /usr/local/bin/iii-target-abi-probe
COPY deployment/targets/probe/runtime_probe.py /usr/local/bin/iii-target-runtime-probe
ENV III_TARGET_ID=raspberry-pi-5-noble-arm64 \
    III_TARGET_IMAGE_PLATFORM_DIGEST=${TARGET_PLATFORM_DIGEST} \
    ROS_DISTRO=jazzy
ENTRYPOINT ["/usr/bin/python3", "/usr/local/bin/iii-target-runtime-probe"]

FROM toolchain AS cross-compiler
ENTRYPOINT ["/entrypoint.sh"]
