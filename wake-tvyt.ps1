# wake-tvyt.ps1 — включить сервер tvyt по локальной сети (Wake-on-LAN)
#
#   powershell -ExecutionPolicy Bypass -File F:\tvyt-deploy\wake-tvyt.ps1
#
# Шлёт magic packet на широковещательный адрес. Сервер должен быть
# подключён кабелем к той же сети, а на нём разрешён Wake-on-LAN.

param(
    [string]$Mac      = "98-4B-E1-95-38-CF",
    [string]$Broadcast = "192.168.0.255",
    [int]$Port        = 9
)

$bytes = [byte[]]($Mac -split '[-:]' | ForEach-Object { [Convert]::ToByte($_, 16) })
if ($bytes.Length -ne 6) {
    Write-Error "MAC должен состоять из 6 байт: $Mac"
    exit 1
}

# 6 байт 0xFF + MAC, повторённый 16 раз = magic packet
$packet = [System.Collections.Generic.List[byte]]::new()
1..6 | ForEach-Object { $packet.Add(255) }
1..16 | ForEach-Object { $packet.AddRange($bytes) }

$udp = New-Object System.Net.Sockets.UdpClient
try {
    $udp.EnableBroadcast = $true
    $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($Broadcast), $Port)
    [void]$udp.Send($packet.ToArray(), $packet.Count, $ep)
    # дублируем на стандартный порт 7 — некоторые карты слушают только его
    $ep7 = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($Broadcast), 7)
    [void]$udp.Send($packet.ToArray(), $packet.Count, $ep7)
    Write-Host "magic packet отправлен: $Mac -> $Broadcast ports $Port,7"
} finally {
    $udp.Close()
}

# Проверяем, поднялся ли сервер (примерно 40-60 секунд на загрузку)
Write-Host "жду загрузку..."
$addr = "192.168.0.50"
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 3
    if (Test-Connection $addr -Count 1 -Quiet -ErrorAction SilentlyContinue) {
        Write-Host "сервер онлайн: $addr (через $([math]::Round(($i+1)*3)) c)"
        exit 0
    }
}
Write-Warning "не ответил за 2 минуты: проверь кабель, BIOS и настройку WoL"
exit 1
