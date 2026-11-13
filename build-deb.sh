#!/bin/sh
# Build the AppStore package. Needs dpkg-deb and python3-pil, so run it on the
# CardputerZero (or any Debian box):  sh build-deb.sh "Name <email>"
# Output: zero-updates_<version>_all.deb in this folder.
set -eu
cd "$(dirname "$0")"
VERSION=$(python3 -c "import json; print(json.load(open('app-builder.json'))['version'])")
PKG=zero-updates
MAINTAINER=${1:?usage: build-deb.sh "Name <email>"}

umask 022
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
chmod 755 "$ROOT"
APP=$ROOT/usr/share/$PKG
L=$ROOT/usr/share/APPLaunch
mkdir -p "$APP" "$L/bin" "$L/applications" "$L/share/images" "$ROOT/DEBIAN"

install -m 644 zero_updates.py registry.py installer.py views.py theme.py keys.py \
    hold_exit.py "$APP/"
printf '#!/bin/sh\nexec /usr/bin/python3 /usr/share/%s/zero_updates.py "$@"\n' "$PKG" > "$L/bin/$PKG"
chmod 755 "$L/bin/$PKG"
install -m 644 zero-updates.desktop "$L/applications/$PKG.desktop"
python3 zero_updates.py --make-icon "$L/share/images/$PKG.png"
chmod 644 "$L/share/images/$PKG.png"

cat > "$ROOT/DEBIAN/control" <<CTRL
Package: $PKG
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Depends: python3, python3-pil, python3-numpy, sudo, apt, dpkg
Section: utils
Priority: optional
Installed-Size: $(du -sk "$ROOT/usr" | cut -f1)
Description: Update installed AppStore apps on CardputerZero
 Lists the AppStore apps installed on the device with the installed and the
 latest version from the official store registry, and updates the picked
 ones, or all of them, after checking each package's SHA-256.
CTRL

dpkg-deb --root-owner-group --build "$ROOT" "${PKG}_${VERSION}_all.deb"
