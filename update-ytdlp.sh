#!/bin/sh
# update-ytdlp.sh — обновление yt-dlp до последнего релиза.
# Запускается systemd-таймером (tvyt-update.timer) раз в неделю.
#
# Скачиваем во временный файл, проверяем что он запускается, и только потом
# подменяем. Если GitHub недоступен или файл битый — остаётся рабочая версия.
set -e

BIN=/usr/local/bin/yt-dlp
NEW=/usr/local/bin/yt-dlp.new
URL=https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux

old=$("$BIN" --version 2>/dev/null || echo none)
echo "текущая версия: $old"

if ! curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 10 --max-time 300 \
     -o "$NEW" "$URL"; then
  echo "скачать не удалось — оставляю $old"
  rm -f "$NEW"
  exit 0
fi

chmod +x "$NEW"
new=$("$NEW" --version 2>/dev/null || echo broken)

case "$new" in
  [0-9]*.*)
    ;;
  *)
    echo "скачанный файл не запускается ('$new') — откат, оставляю $old"
    rm -f "$NEW"
    exit 1
    ;;
esac

if [ "$new" = "$old" ]; then
  echo "уже последняя версия ($new)"
  rm -f "$NEW"
  exit 0
fi

mv -f "$NEW" "$BIN"
chmod +x "$BIN"
echo "обновлено: $old -> $new"
