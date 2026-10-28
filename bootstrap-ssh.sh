#!/bin/sh
# tvyt bootstrap: ставит openssh-server, добавляет ключ, печатает отчёт
KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIfYfHcI4FIUeLDIRkjevwQSQTUaGyHjP/YpIlWses7S tvyt-deploy'
LOG=/root/tvyt-setup.log
: > "$LOG" 2>/dev/null
say() { printf '%-8s %s\n' "$1" "$2"; }

say ENV  "$(uname -srm)"
if [ -r /etc/os-release ]; then
  . /etc/os-release
  say OS "$PRETTY_NAME / codename=${VERSION_CODENAME:-?}"
fi
say PY   "$(python3 -V 2>&1 || echo 'python3 MISSING')"
say ARCH "$(dpkg --print-architecture 2>/dev/null || uname -m)"
say DISK "$(df -h / 2>/dev/null | tail -n1)"
say NET  "$(ip -br a 2>/dev/null | tr '\n' ' ')"
say GW   "$(ip route 2>/dev/null | awk '/default/{print $3; exit}')"

if ping -c1 -W2 8.8.8.8 >/dev/null 2>&1; then
  say INET "yes"
else
  say INET "no"
fi

APT="apt-get -o Acquire::http::Timeout=8 -o Acquire::https::Timeout=8 -o Acquire::Retries=0"

if command -v sshd >/dev/null 2>&1; then
  say SSHD "already installed"
else
  say SSHD "installing via apt (может занять минуту)..."
  $APT update -qq            >>"$LOG" 2>&1
  if $APT install -y -qq openssh-server >>"$LOG" 2>&1; then
    say SSHD "apt ok"
  else
    say SSHD "APT FAILED"
    tail -n 6 "$LOG"
  fi
fi

if command -v sshd >/dev/null 2>&1; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  grep -qF "tvyt-deploy" /root/.ssh/authorized_keys 2>/dev/null || echo "$KEY" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  systemctl enable --now ssh >>"$LOG" 2>&1 || service ssh start >>"$LOG" 2>&1
  say SERVICE "$(systemctl is-active ssh 2>/dev/null || echo unknown)"
  say LISTEN  "$(ss -ltn 2>/dev/null | grep -c ':22 ' || echo 0)"
else
  say SSHD "нет sshd — нужны локальные .deb, скажи об этом агенту"
fi

say HOST "$(hostname)"
say DONE "ok"
