ARG ROS_DISTRO=jazzy

FROM ros:${ROS_DISTRO}-ros-base

ARG ROS_DISTRO

# Make transient Ubuntu mirror failures less likely to abort long image builds.
RUN printf '%s\n' \
    'Acquire::Retries "5";' \
    'Acquire::http::Timeout "30";' \
    'Acquire::https::Timeout "30";' \
    'Acquire::http::Pipeline-Depth "0";' \
    > /etc/apt/apt.conf.d/80-iii-network

# This network blocks Ubuntu HTTP mirrors on port 80. Use HTTPS for Ubuntu
# archive/security sources, but leave ROS sources on HTTP because packages.ros.org
# does not present a certificate valid for that host.
RUN set -eux; \
    for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \
        [ -f "${file}" ] || continue; \
        sed -i -E \
            -e 's|http://([A-Za-z0-9.-]*archive\.ubuntu\.com/ubuntu)|https://\1|g' \
            -e 's|http://(security\.ubuntu\.com/ubuntu)|https://\1|g' \
            "${file}"; \
    done

# Create the user
RUN groupadd --gid 1000 iii && \
    useradd --uid 1000 --gid 1000 -m iii && \
    mkdir -p /home/iii/ws/src && \
    chown -R iii:iii /home/iii/ws && \
    ln -s / /arm64-sysroot

# Install stable apt dependencies in one layer. Avoid full apt upgrades: the
# base image pins the OS/ROS baseline, while upgrades are slow and
# non-reproducible.
RUN apt-get update && apt-get install -y --no-install-recommends \
    sudo \
    ca-certificates \
    ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
    ros-${ROS_DISTRO}-generate-parameter-library \
    ros-${ROS_DISTRO}-generate-parameter-library-py \
    ros-${ROS_DISTRO}-parameter-traits \
    python3-colcon-common-extensions \
    python3-pip \
    tmux \
    vim \
    git \
    build-essential \
    cmake && \
    rm -rf /var/lib/apt/lists/*

RUN echo "iii ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/iii && \
    chmod 0440 /etc/sudoers.d/iii && \
    usermod -s /bin/bash iii && \
    usermod -a -G dialout iii && \
    usermod -a -G video iii

WORKDIR /arm64-sysroot/home/iii/ws

COPY requirements.txt /requirements.txt
RUN chown -R iii:iii /requirements.txt

USER iii

RUN pip3 install --upgrade pip && \
    pip3 install -r /requirements.txt

USER root

RUN rm -rf /requirements.txt

# Install cli
COPY tools/III-Drone-CLI /III-Drone-CLI
RUN chown -R iii:iii /III-Drone-CLI
USER iii
RUN pip3 install /III-Drone-CLI
USER root
RUN rm -rf /III-Drone-CLI

RUN echo "export PATH=$PATH:/home/iii/.local/bin" >> /home/iii/.bashrc

RUN activate-global-python-argcomplete3

USER iii

RUN if ! grep -q "eval \"\$(register-python-argcomplete3 iii)\"" ~/.bashrc; then \
        echo "eval \"\$(register-python-argcomplete3 iii)\"" >> ~/.bashrc ; \
    fi

ENV SHELL=/bin/bash

# Copy the entrypoint script
COPY entrypoint_real.sh /entrypoint.sh
USER root
RUN chmod +x /entrypoint.sh

# Workspace runtime/build dependencies. Keep this late so adding package
# dependencies does not invalidate the stable tooling, Python, or CLI layers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-pcl-ros \
    libopencv-dev \
    python3-opencv \
    ros-${ROS_DISTRO}-cv-bridge \
    ros-${ROS_DISTRO}-image-transport \
    ros-${ROS_DISTRO}-usb-cam \
    libzmq3-dev && \
    rm -rf /var/lib/apt/lists/*

USER iii

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]
