#!/bin/sh
# deploy.sh — установка tvyt на чистый Debian (запускать от root на самом сервере)
#   sh deploy.sh
# Ставит ffmpeg и yt-dlp (если их нет), копирует приложение и юнит, включает сервис.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" != "0" ]; then
  echo "нужен root: sudo sh $0" >&2
  exit 1
fi

echo "== пакеты =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
command -v ffmpeg >/dev/null 2>&1 || apt-get install -y -qq ffmpeg
command -v ethtool >/dev/null 2>&1 || apt-get install -y -qq ethtool
if ! command -v yt-dlp >/dev/null 2>&1; then
  curl -sL -o /usr/local/bin/yt-dlp \
    https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux
  chmod +x /usr/local/bin/yt-dlp
fi

echo "== приложение =="
mkdir -p /opt/tvyt/cache
cp "$HERE/app.py" /opt/tvyt/app.py
cp "$HERE/tvyt.service" /etc/systemd/system/tvyt.service
systemctl daemon-reload
systemctl enable tvyt >/dev/null 2>&1 || true
systemctl restart tvyt

echo "== автообновление yt-dlp =="
cp "$HERE/update-ytdlp.sh" /usr/local/bin/update-ytdlp.sh
chmod +x /usr/local/bin/update-ytdlp.sh
cp "$HERE/tvyt-update.service" "$HERE/tvyt-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tvyt-update.timer >/dev/null 2>&1 || true

echo "== wake-on-lan =="
cp "$HERE/wol-enable.sh" /usr/local/bin/wol-enable.sh
chmod +x /usr/local/bin/wol-enable.sh
cp "$HERE/tvyt-wol.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable tvyt-wol >/dev/null 2>&1 || true
systemctl restart tvyt-wol || true

echo "== проверка =="
sleep 2
systemctl is-active tvyt
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1/tv
ip -br a | grep -v '^lo'
echo
echo "Адрес для телевизора: http://<этот-ip>/tv"
echo "Адрес для телефона:   http://<этот-ip>/"
