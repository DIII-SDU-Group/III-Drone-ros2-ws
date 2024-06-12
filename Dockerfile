FROM ros:humble-ros-base

# Create the user
RUN groupadd --gid 1000 iii
RUN useradd --uid 1000 --gid 1000 -m iii
RUN apt-get update
RUN apt-get install -y sudo
RUN echo iii ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/iii
RUN chmod 0440 /etc/sudoers.d/iii
RUN apt-get update && apt-get upgrade -y
RUN usermod -s /bin/bash iii

# Install dependencies
COPY src/III-Drone-Core/scripts /scripts

RUN \
    apt update && \
    apt install -y ros-humble-rmw-cyclonedds-cpp && \
    /scripts/install_dependencies.sh && \
    rm -rf /scripts && \
    apt update

# Install necessary packages and dependencies
RUN apt-get update && apt-get install -y \
    python3-colcon-common-extensions \
    python3-pip

RUN apt-get update && apt-get upgrade -y

# Create workspace folder and set ownership
RUN mkdir -p /home/iii/ws/src
RUN chown -R iii:iii /home/iii/ws

RUN ln -s / /arm64-sysroot

WORKDIR /arm64-sysroot/home/iii/ws

# Install userspace tools
RUN apt install -y tmux tmuxinator vim

COPY requirements.txt /requirements.txt

RUN chown -R iii:iii /requirements.txt

USER iii

RUN pip3 install --upgrade pip
RUN pip3 install -r /requirements.txt

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

ENV SHELL /bin/bash

# Prepare tmux configuration
RUN mkdir -p /home/iii/.config/tmuxinator

# Copy the entrypoint script
COPY entrypoint_real.sh /entrypoint.sh
USER root
RUN chmod +x /entrypoint.sh

# Add user to dialout and video groups
RUN usermod -a -G dialout iii
RUN usermod -a -G video iii

USER iii

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]
