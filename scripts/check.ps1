# 一条命令运行 lint、格式检查、类型检查和测试（Windows PowerShell）。
# 用法：在项目根目录执行  .\scripts\check.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# uv 不在 PATH 上时（例如用 pip 装在 Python 的 Scripts 目录下），退回到 py -3.13 -m uv（与 .python-version 一致）
if (Get-Command uv -ErrorAction SilentlyContinue) { $uv = @("uv") } else { $uv = @("py", "-3.13", "-m", "uv") }

$steps = @(
    @("run", "ruff", "check", "."),
    @("run", "ruff", "format", "--check", "."),
    @("run", "mypy"),
    @("run", "pytest")
)
foreach ($step in $steps) {
    $cmd = $uv + $step
    Write-Host ">> $($cmd -join ' ')"
    & $cmd[0] $cmd[1..($cmd.Length - 1)]
    if ($LASTEXITCODE -ne 0) {
        Write-Host "检查失败：$($cmd -join ' ')" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}
Write-Host "全部检查通过" -ForegroundColor Green
