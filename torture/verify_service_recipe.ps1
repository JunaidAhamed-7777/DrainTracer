param(
    [string]$Python = 'python',
    [string]$ResultPath = (Join-Path $env:TEMP 'draintracer-service-recipe-result.json')
)

$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'The disposable service test needs an elevated terminal on a disposable Windows runner.'
}
$Repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ServiceName = 'DrainTracerRecipe_' + [guid]::NewGuid().ToString('N')
$ServiceRoot = Join-Path $env:TEMP $ServiceName
$Executable = Join-Path $ServiceRoot 'DisposableService.exe'
$BinaryPath = '"' + $Executable + '" ' + $ServiceName
$Created = $false
$ProcessIdentity = $null

function Invoke-Sc([string[]]$Arguments) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Join-Path $env:WINDIR 'System32\sc.exe'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($start)
    try {
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill()
            $process.WaitForExit()
            throw 'The owned service-control request timed out.'
        }
        if ($process.ExitCode -ne 0) {
            throw ('The owned service-control request failed: ' + $output.Result + $errors.Result)
        }
    } finally { $process.Dispose() }
}

function Get-OwnedService {
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if ($null -ne $service -and $service.PathName -ne $BinaryPath) {
        throw 'The service registration no longer matches this disposable fixture; refusing to control it.'
    }
    return $service
}

try {
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        throw 'A generated service name already exists; refusing to replace it.'
    }
    New-Item -ItemType Directory -Path $ServiceRoot | Out-Null
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    & $compiler /nologo /target:exe "/out:$Executable" /reference:System.ServiceProcess.dll (Join-Path $PSScriptRoot 'exe\DisposableService.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Could not compile the disposable service.' }
    # Grant only read/execute on this new fixture folder to LocalService.
    & icacls.exe $ServiceRoot /grant '*S-1-5-19:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not grant LocalService access to its fixture executable.' }
    Invoke-Sc -Arguments @('create', $ServiceName, 'binPath=', $BinaryPath, 'start=', 'demand', 'obj=', 'NT AUTHORITY\LocalService')
    $Created = $true
    Invoke-Sc -Arguments @('start', $ServiceName)
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $service = Get-OwnedService
        if ($service.State -eq 'Running' -and $service.ProcessId -gt 0) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($service.State -ne 'Running' -or $service.ProcessId -le 0) { throw 'Disposable service did not become ready.' }
    $ServicePid = [int]$service.ProcessId
    $ProcessIdentity = Get-Process -Id $ServicePid
    $CreatedAt = $ProcessIdentity.StartTime.ToUniversalTime()
    Invoke-Sc -Arguments @('control', $ServiceName, '128')

    $Reports = Join-Path $ServiceRoot 'reports'
    & $Python (Join-Path $Repository 'src\main.py') --headless --no-config --pid $ServicePid --timeout 16s --warmup 2s --interval 250ms --window 10s --fail-on-inconclusive --workload-id disposable-service --report-dir $Reports
    if ($LASTEXITCODE -ne 0) { throw 'The disposable service observation did not return a judged pass.' }
    $after = Get-OwnedService
    $afterProcess = Get-Process -Id $ServicePid
    if ($after.State -ne 'Running' -or $after.ProcessId -ne $ServicePid -or $afterProcess.StartTime.ToUniversalTime() -ne $CreatedAt) {
        throw 'Ordinary monitor detach did not leave the same service process running.'
    }
    $json = @(Get-ChildItem -LiteralPath $Reports -Recurse -Filter '*.json')
    if ($json.Count -ne 1) { throw 'Expected one exported service report.' }
    $report = Get-Content -LiteralPath $json[0].FullName -Raw | ConvertFrom-Json
    if (-not $report.detection_ran -or $report.mode -ne 'attach' -or $report.root_pid -ne $ServicePid -or $report.outcome.target.state -ne 'left_running') {
        throw 'The report did not preserve judged attached-observation semantics.'
    }
    if (-not (Test-Path -LiteralPath ([IO.Path]::ChangeExtension($json[0].FullName, '.html')))) {
        throw 'The matching HTML report was not exported.'
    }
    $result = [ordered]@{
        environment = [Environment]::OSVersion.VersionString
        python = (& $Python --version | Out-String).Trim()
        duration_seconds = 16
        warmup_seconds = 2
        window_seconds = 10
        samples = $report.sample_count
        detection_ran = $report.detection_ran
        partial_coverage = $report.coverage.partial
        same_service_running_after_detach = $true
        account = 'LocalService'
        fixture_self_limit_seconds = 120
    }
} finally {
    if ($Created) {
        $owned = Get-OwnedService
        if ($null -ne $owned) {
            if ($owned.State -ne 'Stopped') {
                Invoke-Sc -Arguments @('stop', $ServiceName)
                $controller = Get-Service -Name $ServiceName
                try { $controller.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(15)) }
                finally { $controller.Dispose() }
            }
            Invoke-Sc -Arguments @('delete', $ServiceName)
            $removedBy = [DateTime]::UtcNow.AddSeconds(5)
            while ($null -ne (Get-OwnedService) -and [DateTime]::UtcNow -lt $removedBy) {
                Start-Sleep -Milliseconds 100
            }
            if ($null -ne (Get-OwnedService)) { throw 'Disposable service registration cleanup is incomplete.' }
        }
    }
    if ($null -ne $ProcessIdentity) { $ProcessIdentity.Dispose() }
    # Keep this explicitly named temporary fixture/report folder for inspection.
}
$result['registration_removed'] = $true
$result | ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding utf8
$result | ConvertTo-Json
