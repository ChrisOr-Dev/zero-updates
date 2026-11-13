#!/bin/sh
# Register Updates with APPLaunch. Copy this folder to ~/zero-updates on the
# CardputerZero, then run:  sudo sh ~/zero-updates/install.sh
# The launcher entry is a small wrapper owned by pi; the app itself stays in
# ~/zero-updates, so later updates need no sudo.
set -eu
cd "$(dirname "$0")"
L=/usr/share/APPLaunch
APP_DIR=$(pwd)
printf '#!/bin/sh\nexec /usr/bin/python3 %s/zero_updates.py "$@"\n' "$APP_DIR" > "$L/bin/zero-updates"
chown pi:pi "$L/bin/zero-updates"
chmod 755 "$L/bin/zero-updates"
install -m 644 zero-updates.desktop "$L/applications/zero-updates.desktop"
python3 zero_updates.py --make-icon "$L/share/images/zero-updates.png"
chmod 644 "$L/share/images/zero-updates.png"
echo "Installed. Restart the launcher as pi: systemctl --user restart APPLaunch"
