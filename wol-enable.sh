#!/bin/sh
# wol-enable.sh — разрешает Wake-on-LAN на сетевой карте.
# Запускается systemd-юнитом tvyt-wol.service при каждой загрузке:
# настройка ethtool сбрасывается после перезагрузки.
IFACE=enp3s0

[ -d "/sys/class/net/$IFACE" ] || exit 0

# 1. Разрешаем карте принимать magic packet (g = magic packet).
ethtool -s "$IFACE" wol g >/dev/null 2>&1 || true

# 2. Разрешаем устройству будить систему из PCI (ACPI).
if [ -w "/sys/class/net/$IFACE/device/power/wakeup" ]; then
  echo enabled > "/sys/class/net/$IFACE/device/power/wakeup" 2>/dev/null || true
fi

# 3. Разрешаем пробуждение из S5 (полное выключение) на уровне ACPI.
#    /proc/acpi/wakeup — переключатель: запись меняет состояние на обратное,
#    поэтому сначала проверяем, не включено ли уже.
if grep -q 'PXSX.*\*disabled' /proc/acpi/wakeup 2>/dev/null; then
  echo PXSX > /proc/acpi/wakeup 2>/dev/null || true
fi

echo "wol: $(ethtool "$IFACE" 2>/dev/null | grep -i 'Wake-on:' | tr -d '\t')"
grep 'PXSX' /proc/acpi/wakeup 2>/dev/null || true
