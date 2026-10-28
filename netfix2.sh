#!/bin/sh
# Убираем старый DHCP-адрес, оставляем только статический .50
ip addr del 192.168.0.204/24 dev enp3s0 2>/dev/null || true
ip route replace default via 192.168.0.1 dev enp3s0 src 192.168.0.50 metric 1002 2>/dev/null || true
systemctl restart dhcpcd 2>/dev/null || true
sleep 3
ip addr del 192.168.0.204/24 dev enp3s0 2>/dev/null || true
ip -br a show enp3s0
ip r
