FROM --platform=linux/arm64 iii_drone_base:latest as ROS

FROM ros:humble-ros-base

# Copy the sys root
# COPY --from=ROS /opt/ros/humble /opt/ros/humble
# COPY --from=ROS /usr/lib/aarch64-linux-gnu /usr/lib/aarch64-linux-gnu
COPY --from=ROS / /arm64-sysroot
RUN rm -rf /opt/ros/humble
RUN ln -s /arm64-sysroot/opt/ros/humble /opt/ros/humble

# RUN apt-get update && apt-get install -y \
#     python3-colcon-common-extensions

# RUN apt-get update && apt-get upgrade -y

# Create the user
RUN groupadd --gid 1000 iii
RUN useradd --uid 1000 --gid 1000 -m iii
RUN apt-get update
RUN apt-get install -y sudo
RUN echo iii ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/iii
RUN chmod 0440 /etc/sudoers.d/iii
# RUN apt-get update && apt-get upgrade -y
RUN usermod -s /bin/bash iii

# Setup cross compilation
RUN apt install -y g++-aarch64-linux-gnu gcc-aarch64-linux-gnu
RUN cp /usr/aarch64-linux-gnu/lib/ld-linux-aarch64.so.1 /lib
RUN cp /usr/aarch64-linux-gnu/lib/libc.so.6 /lib

RUN rm /arm64-sysroot/usr/bin/python3
RUN ln -s /usr/bin/python3 /arm64-sysroot/usr/bin/python3
RUN ln -s /arm64-sysroot/usr/lib/aarch64-linux-gnu/libpython3.10.so /usr/lib/aarch64-linux-gnu/
RUN rm /arm64-sysroot/usr/bin/git
RUN ln -s /usr/bin/git /arm64-sysroot/usr/bin/git
RUN rm -r /arm64-sysroot/home/iii
RUN ln -s /home/iii /arm64-sysroot/home/iii

# Update symbolic links
COPY cc_ws/update_symlinks.bash /update_symlinks.bash
RUN chmod +x /update_symlinks.bash
RUN /update_symlinks.bash /arm64-sysroot /arm64-sysroot/usr
RUN /update_symlinks.bash /arm64-sysroot /arm64-sysroot/lib
RUN /update_symlinks.bash /arm64-sysroot /arm64-sysroot/bin
RUN /update_symlinks.bash /arm64-sysroot /arm64-sysroot/etc
RUN rm /update_symlinks.bash

RUN ln -s /arm64-sysroot/usr/lib/libOpenNI.so /usr/lib/libOpenNI.so
RUN ln -s /arm64-sysroot/usr/lib/aarch64-linux-gnu/libOpenNI2.so /usr/lib/aarch64-linux-gnu/libOpenNI2.so
RUN ln -s /arm64-sysroot/usr/lib/aarch64-linux-gnu/libpcl_common.so /usr/lib/aarch64-linux-gnu/libpcl_common.so

# Link libs to sysroot
# COPY cc_ws/link_libs.bash /link_libs.bash
# RUN chmod +x /link_libs.bash
# RUN /link_libs.bash /arm64-sysroot usr/lib
# RUN /link_libs.bash /arm64-sysroot usr/lib/aarch64-linux-gnu
# RUN rm /link_libs.bash

# Link includes to sysroot
COPY cc_ws/link_includes.bash /link_includes.bash
RUN chmod +x /link_includes.bash
RUN /link_includes.bash /arm64-sysroot usr/include
RUN rm /link_includes.bash

# Create workspace folder and set ownership
RUN mkdir -p /home/iii/ws/src
RUN chown -R iii:iii /home/iii/ws

WORKDIR /home/iii/ws

# RUN echo "export TARGET_ARCH=aarch64" >> /home/iii/.bashrc
# RUN echo "export TARGET_TRIPLE=aarch64-linux-gnu" >> /home/iii/.bashrc
# RUN echo 'export CC=/usr/bin/$TARGET_TRIPLE-gcc' >> /home/iii/.bashrc
# RUN echo 'export CXX=/usr/bin/$TARGET_TRIPLE-g++' >> /home/iii/.bashrc

# # Install userspace tools
# RUN apt install -y tmux tmuxinator vim

# COPY requirements.txt /requirements.txt

# RUN chown -R iii:iii /requirements.txt

# USER iii

# RUN pip3 install --upgrade pip
# RUN pip3 install -r /requirements.txt

# USER root

# RUN rm -rf /requirements.txt

# # Install cli
# COPY tools/III-Drone-CLI /III-Drone-CLI
# RUN chown -R iii:iii /III-Drone-CLI
# USER iii
# RUN pip3 install /III-Drone-CLI
# USER root
# RUN rm -rf /III-Drone-CLI

# RUN activate-global-python-argcomplete3

# USER iii

# RUN if ! grep -q "eval \"\$(register-python-argcomplete3 iii)\"" ~/.bashrc; then \
#         echo "eval \"\$(register-python-argcomplete3 iii)\"" >> ~/.bashrc ; \
#     fi

ENV SHELL /bin/bash

# # Prepare tmux configuration
# RUN mkdir -p /home/iii/.config/tmuxinator

# Copy the entrypoint script
COPY entrypoint_cc.sh /entrypoint.sh
USER root
RUN chmod +x /entrypoint.sh

USER iii

# Set the entrypoint
ENTRYPOINT ["/entrypoint.sh"]
