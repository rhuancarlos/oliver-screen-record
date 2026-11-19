#!/usr/bin/env bash
# Instalador do Oliver Screen Record (Ubuntu/Debian >= 25.04)
set -euo pipefail

APP="oliver-screen-record"
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"

DEPS=(
  python3 python3-gi
  gir1.2-gtk-4.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
  gstreamer1.0-pipewire gstreamer1.0-pulseaudio
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
  pulseaudio-utils xdg-desktop-portal
)

# Backend do portal conforme o ambiente gráfico
case "${XDG_CURRENT_DESKTOP:-}" in
  *KDE*)                      DEPS+=(xdg-desktop-portal-kde) ;;
  *GNOME*|*Unity*|*ubuntu*)   DEPS+=(xdg-desktop-portal-gnome) ;;
  *sway*|*Hyprland*|*wlroots*) DEPS+=(xdg-desktop-portal-wlr) ;;
  *)                          DEPS+=(xdg-desktop-portal-gnome) ;;
esac

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$BIN_DIR/$APP" "$APPS_DIR/$APP.desktop"
  echo "Oliver Screen Record removido (configurações em ~/.config/$APP mantidas)."
  exit 0
fi

echo ">> Instalando dependências (sudo)…"
sudo apt-get update -q
sudo apt-get install -y "${DEPS[@]}"
# Janela da câmera (gst-plugins-rs)
sudo apt-get install -y gstreamer1.0-gtk4 2>/dev/null || echo "   (gstreamer1.0-gtk4 indisponível: a gravação funciona, mas sem câmera)"
# Opcional: câmeras que só funcionam via libcamera (ex.: MIPI/IPU6 de notebooks recentes)
sudo apt-get install -y gstreamer1.0-libcamera 2>/dev/null || echo "   (gstreamer1.0-libcamera indisponível, ignorando)"

echo ">> Copiando programa…"
install -Dm755 "$DIR/$APP.py" "$BIN_DIR/$APP"

mkdir -p "$APPS_DIR"
cat > "$APPS_DIR/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Oliver Screen Record
Comment=Gravação de tela com áudio do sistema, microfone e câmera
Exec=$BIN_DIR/$APP
Icon=media-record
Terminal=false
Categories=AudioVideo;Video;Recorder;Utility;
Keywords=screen;record;recorder;webcam;gravar;tela;camera;
StartupNotify=true
EOF
update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo ">> Verificando plugins GStreamer…"
"$BIN_DIR/$APP" --check || true

echo
echo "Pronto! Abra pelo menu de aplicativos (\"Oliver Screen Record\") ou rode: $APP"
[[ ":$PATH:" == *":$BIN_DIR:"* ]] || echo "Obs.: adicione $BIN_DIR ao PATH para usar no terminal."
