ARG ROS_DISTRO

# Use an official ROS2 base image
# FROM ros:${ROS_DISTRO}-ros-core
FROM ros:${ROS_DISTRO}-ros-base

# Create the user
RUN groupadd --gid 1000 iii
RUN useradd --uid 1000 --gid 1000 -m iii
RUN apt-get update
RUN apt-get install -y sudo
RUN echo iii ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/iii
RUN chmod 0440 /etc/sudoers.d/iii
RUN apt-get update && apt-get upgrade -y

# Install necessary packages and dependencies
RUN apt-get update && apt-get install -y \
    python3-colcon-common-extensions \
    python3-pip

RUN apt-get update && apt-get upgrade -y

# Install dependencies
COPY src/III-Drone-Core/scripts /scripts
RUN /scripts/install_dependencies.sh
RUN rm -rf /scripts

# Copy the entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Create workspace folder and set ownership
RUN mkdir -p /home/iii/ws/src

WORKDIR /home/iii/ws

COPY src /home/iii/ws/src
COPY install /home/iii/ws/install
COPY build /home/iii/ws/build
COPY log /home/iii/ws/log

RUN chown -R iii:iii /home/iii/ws

# Install configuration files
RUN bash -c "./src/III-Drone-Configuration/scripts/install.sh /home/iii/.config"

# Setup the environment
RUN echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> /etc/bash.bashrc
RUN echo "source /home/iii/ws/install/setup.bash" >> /etc/bash.bashrc
RUN echo "export CONFIG_BASE_DIR=/home/iii/.config" >> /etc/bash.bashrc
RUN echo "export SIMULATION=false" >> /etc/bash.bashrc

ENV SHELL /bin/bash

USER iii

# Build ROS2 workspace
RUN bash -c "source /opt/ros/${ROS_DISTRO}/setup.bash && rosdep update && rosdep install --from-paths src --ignore-src -y"
RUN bash -c "source /opt/ros/${ROS_DISTRO}/setup.bash && colcon build --symlink-install"
