FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        netplan.io \
        openssh-server \
        python3 \
        python3-apt \
        sudo \
        systemd \
        systemd-sysv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl enable ssh.service \
    && systemctl mask systemd-logind.service getty.target console-getty.service

# Docker's /run tmpfs starts empty. Canonical's boot stack normally creates
# this path before sshd; make that lifecycle explicit in the systemd harness.
RUN mkdir -p /etc/systemd/system/ssh.service.d \
    && printf '%s\n' \
        '[Service]' \
        'RuntimeDirectory=sshd' \
        'RuntimeDirectoryMode=0755' \
        'ExecStartPre=' \
        'ExecStartPre=/usr/bin/install -d -m 0755 /run/sshd' \
        'ExecStartPre=/usr/sbin/sshd -t' \
        > /etc/systemd/system/ssh.service.d/runtime-directory.conf

# binfmt_misc is a host-shared kernel filesystem even with a container mount
# namespace. Never let the privileged systemd harness rewrite host handlers.
RUN systemctl mask systemd-binfmt.service

# Canonical's Raspberry Pi server image includes the HTTPS trust bootstrap.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/sbin/init"]
