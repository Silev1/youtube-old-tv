#!/bin/sh
# Применяем статический IP 192.168.0.50 и починяем автозапуск dhcpcd
set -e

cat > /etc/systemd/system/dhcpcd.service <<'EOF'
[Unit]
Description=DHCP Client Daemon (static config in /etc/dhcpcd.conf)
After=network.target
Wants=network.target

[Service]
Type=forking
ExecStart=/usr/sbin/dhcpcd -q -b enp3s0
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable dhcpcd >/dev/null 2>&1 || true

pkill -x dhcpcd 2>/dev/null || true
sleep 2
systemctl restart dhcpcd 2>/dev/null || systemctl start dhcpcd 2>/dev/null || true
sleep 3

ip -br a show enp3s0
systemctl is-enabled dhcpcd
systemctl is-active dhcpcd
