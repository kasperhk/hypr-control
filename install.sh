#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX=${PREFIX:-"$HOME/.local"}
APP_DIR="$PREFIX/lib/hypr-control"
BIN_DIR="$PREFIX/bin"
APPLICATIONS_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/applications"
ICONS_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/icons/hicolor/scalable/apps"
METAINFO_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/metainfo"
CONFIG_HOME="${XDG_CONFIG_HOME:-"$HOME/.config"}"
SYSTEMD_DIR="$CONFIG_HOME/systemd/user"
HYPR_DIR="$CONFIG_HOME/hypr"
HYPR_CONTROL_CONFIG_DIR="$CONFIG_HOME/hypr-control"

install -d "$APP_DIR/hypr_control_center/displays" "$BIN_DIR" "$APPLICATIONS_DIR" "$ICONS_DIR" "$METAINFO_DIR" "$SYSTEMD_DIR" "$HYPR_DIR" "$HYPR_CONTROL_CONFIG_DIR"
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

install_managed_file() {
    source=$1
    target=$2
    if [ ! -e "$target" ]; then
        install -m644 "$source" "$target"
    fi
}

install_managed_file "$PROJECT_DIR/data/hypr-control.lua" "$HYPR_DIR/hypr-control.lua"
install_managed_file "$PROJECT_DIR/data/hypr-control-monitors.lua" "$HYPR_DIR/hypr-control-monitors.lua"
install_managed_file "$PROJECT_DIR/data/hypr-control-bindings.lua" "$HYPR_DIR/hypr-control-bindings.lua"
install_managed_file "$PROJECT_DIR/data/hypr-control-bindings.conf" "$HYPR_CONTROL_CONFIG_DIR/bindings.conf"

HYPRLAND_CONFIG="$HYPR_DIR/hyprland.lua"
if [ -f "$HYPRLAND_CONFIG" ]; then
    missing_modules=""
    for module in hypr-control-monitors hypr-control hypr-control-bindings; do
        require_line="require(\"hypr.$module\")"
        if ! grep -Fqx "$require_line" "$HYPRLAND_CONFIG"; then
            missing_modules="$missing_modules$require_line
"
        fi
    done
    if [ -n "$missing_modules" ]; then
        backup="$HYPRLAND_CONFIG.bak.hypr-control-$(date +%Y%m%d-%H%M%S)"
        cp -p "$HYPRLAND_CONFIG" "$backup"
        {
            printf '\n-- Hypr Control native-Lua modules.\n'
            printf '%s' "$missing_modules"
        } >> "$HYPRLAND_CONFIG"
        printf 'Hyprland integration added; backup: %s\n' "$backup"
    fi
else
    printf 'Hyprland Lua config was not found; add the Hypr Control modules after migrating Omarchy.\n' >&2
fi

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
