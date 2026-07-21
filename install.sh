#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX=${PREFIX:-"$HOME/.local"}
APP_DIR="$PREFIX/lib/hypr-control"
BIN_DIR="$PREFIX/bin"
APPLICATIONS_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/applications"
ICONS_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/icons/hicolor/scalable/apps"
METAINFO_DIR="${XDG_DATA_HOME:-"$HOME/.local/share"}/metainfo"

install -d "$APP_DIR/hypr_control_center" "$BIN_DIR" "$APPLICATIONS_DIR" "$ICONS_DIR" "$METAINFO_DIR"
install -m755 "$PROJECT_DIR/hypr-control-center" "$APP_DIR/hypr-control-center"
install -m644 "$PROJECT_DIR/hypr_control_center/__init__.py" "$APP_DIR/hypr_control_center/__init__.py"
install -m644 "$PROJECT_DIR/hypr_control_center/__main__.py" "$APP_DIR/hypr_control_center/__main__.py"
install -m644 "$PROJECT_DIR/hypr_control_center/app.py" "$APP_DIR/hypr_control_center/app.py"
install -m644 "$PROJECT_DIR/hypr_control_center/config_engine.py" "$APP_DIR/hypr_control_center/config_engine.py"
install -m644 "$PROJECT_DIR/hypr_control_center/style.css" "$APP_DIR/hypr_control_center/style.css"
ln -sfn "$APP_DIR/hypr-control-center" "$BIN_DIR/hypr-control-center"
install -m644 "$PROJECT_DIR/data/io.github.kasperhk.HyprControl.desktop" "$APPLICATIONS_DIR/io.github.kasperhk.HyprControl.desktop"
install -m644 "$PROJECT_DIR/data/io.github.kasperhk.HyprControl.svg" "$ICONS_DIR/io.github.kasperhk.HyprControl.svg"
install -m644 "$PROJECT_DIR/data/io.github.kasperhk.HyprControl.metainfo.xml" "$METAINFO_DIR/io.github.kasperhk.HyprControl.metainfo.xml"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR"
fi

printf 'Hypr Control installed. Run: %s\n' "$BIN_DIR/hypr-control-center"
