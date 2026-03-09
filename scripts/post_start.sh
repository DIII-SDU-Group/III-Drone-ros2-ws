#!/bin/bash

# Remove previously managed iii argcomplete block (if present) and legacy single-line entries.
sed -i '/# >>> iii-cli argcomplete >>>/,/# <<< iii-cli argcomplete <<</d' ~/.bashrc
sed -i '/# III CLI argcomplete (safe across argcomplete command variants)./,/^fi$/d' ~/.bashrc
sed -i '/eval "\$(register-python-argcomplete3 iii)"/d;/eval "\$(register-python-argcomplete iii)"/d' ~/.bashrc

# Add resilient completion setup once.
if ! grep -q "# >>> iii-cli argcomplete >>>" ~/.bashrc; then
cat >> ~/.bashrc <<'EOF'
# >>> iii-cli argcomplete >>>
__iii_enable_argcomplete() {
    local cmd shellcode
    for cmd in register-python-argcomplete3 register-python-argcomplete; do
        if command -v "$cmd" >/dev/null 2>&1; then
            shellcode="$("$cmd" iii 2>/dev/null || true)"
            if [ -n "$shellcode" ]; then
                eval "$shellcode"
                return 0
            fi
        fi
    done
    return 1
}
__iii_enable_argcomplete || true
unset -f __iii_enable_argcomplete
# <<< iii-cli argcomplete <<<
EOF
fi

# Reinstall the III-Drone-CLI
pip3 uninstall -y iii 2> /dev/null
pip3 install -e ./tools/III-Drone-CLI

# Source bashrc
source ~/.bashrc

# Build workspace
COLCON_HOME=/home/iii/ws colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Debug -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
