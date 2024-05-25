ARG ROS_DISTRO

# Use an official ROS2 base image
FROM ros:${ROS_DISTRO}

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

# Create workspace folder and set ownership
RUN mkdir -p /home/iii/ws/src
RUN chown -R iii:iii /home/iii/ws

WORKDIR /home/iii/ws

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]

RUN echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> /etc/bash.bashrc
RUN echo "source /home/iii/ws/install/setup.bash" >> /etc/bash.bashrc

ENV SHELL /bin/bash

USER iii
