#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX=${PREFIX:-"$HOME/.local"}
APP_DIR="$PREFIX/lib/hypr-control"
BIN_DIR="$PREFIX/bin"
APPLICATIONS_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/applications"
ICONS_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/icons/hicolor/scalable/apps"
METAINFO_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/metainfo"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user"

install -d "$APP_DIR/hypr_control_center/displays" "$BIN_DIR" "$APPLICATIONS_DIR" "$ICONS_DIR" "$METAINFO_DIR" "$SYSTEMD_DIR"
install -m755 "$PROJECT_DIR/hypr-control-center" "$APP_DIR/hypr-control-center"
install -m755 "$PROJECT_DIR/hypr-control-display" "$APP_DIR/hypr-control-display"
install -m644 "$PROJECT_DIR/hypr_control_center/__init__.py" "$APP_DIR/hypr_control_center/__init__.py"
install -m644 "$PROJECT_DIR/hypr_control_center/__main__.py" "$APP_DIR/hypr_control_center/__main__.py"
install -m644 "$PROJECT_DIR/hypr_control_center/app.py" "$APP_DIR/hypr_control_center/app.py"
install -m644 "$PROJECT_DIR/hypr_control_center/config_engine.py" "$APP_DIR/hypr_control_center/config_engine.py"
install -m644 "$PROJECT_DIR/hypr_control_center/style.css" "$APP_DIR/hypr_control_center/style.css"
install -m644 "$PROJECT_DIR"/hypr_control_center/displays/*.py "$APP_DIR/hypr_control_center/displays/"
ln -sfn "$APP_DIR/hypr-control-center" "$BIN_DIR/hypr-control-center"
ln -sfn "$APP_DIR/hypr-control-display" "$BIN_DIR/hypr-control-display"
install -m644 "$PROJECT_DIR/data/io.github.kasperhk.HyprControl.desktop" "$APPLICATIONS_DIR/io.github.kasperhk.HyprControl.desktop"
install -m644 "$PROJECT_DIR/data/io.github.kasperhk.HyprControl.svg" "$ICONS_DIR/io.github.kasperhk.HyprControl.svg"
install -m644 "$PROJECT_DIR/data/io.github.kasperhk.HyprControl.metainfo.xml" "$METAINFO_DIR/io.github.kasperhk.HyprControl.metainfo.xml"
sed "s|@EXEC@|$BIN_DIR/hypr-control-display|" "$PROJECT_DIR/data/io.github.kasperhk.HyprControl.DisplayService.service" > "$SYSTEMD_DIR/io.github.kasperhk.HyprControl.DisplayService.service"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR"
fi

if [ "${HYPR_CONTROL_SKIP_SERVICE:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    if ! systemctl --user enable --now io.github.kasperhk.HyprControl.DisplayService.service; then
        printf 'Display automation was installed but could not be started in this session.\n'
    fi
fi

printf 'Hypr Control installed. Run: %s\n' "$BIN_DIR/hypr-control-center"
