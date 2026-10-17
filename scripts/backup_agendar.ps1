# Agenda o backup diário do PrimeSys no Windows (rodar UMA vez, depois de backup_chaves.py).
# Todo dia às 03:00 baixa o backup criptografado para %USERPROFILE%\PrimeSysBackup\arquivos.
# Se o PC estiver desligado nesse horário, roda assim que ligar. Sem janela na tela.
#
# Uso (PowerShell):  powershell -ExecutionPolicy Bypass -File "D:\PRIMESYS APP\scripts\backup_agendar.ps1"

$projeto = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projeto ".venv\Scripts\pythonw.exe"
$script = Join-Path $projeto "scripts\backup_baixar.py"
if (-not (Test-Path $python)) { Write-Host "Nao encontrei $python"; exit 1 }
if (-not (Test-Path (Join-Path $env:USERPROFILE "PrimeSysBackup\config.json"))) {
    Write-Host "Rode antes: python scripts\backup_chaves.py"; exit 1
}

$acao = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $projeto
$quando = New-ScheduledTaskTrigger -Daily -At 3:00
$opcoes = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "PrimeSys Backup" -Action $acao -Trigger $quando -Settings $opcoes `
    -Description "Baixa o backup criptografado do PrimeSys (app.primesysapp.com.br)" -Force | Out-Null

Write-Host "Tarefa 'PrimeSys Backup' agendada para todo dia as 03:00."
Write-Host "Para testar agora:  Start-ScheduledTask -TaskName 'PrimeSys Backup'"
Write-Host "Resultado de cada execucao: $env:USERPROFILE\PrimeSysBackup\backup.log"
