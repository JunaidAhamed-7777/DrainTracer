param(
    [string]$OutputPath = (Join-Path $PSScriptRoot "fixtures\steady.exe")
)

$ErrorActionPreference = "Stop"

$sourcePath = Join-Path $PSScriptRoot "exe\SteadyFixture.cs"
$compilerCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$compiler = $compilerCandidates |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1

if (-not $compiler) {
    throw "The .NET Framework C# compiler was not found."
}

$resolvedOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    $OutputPath
)
$outputDirectory = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

& $compiler `
    /nologo `
    /optimize+ `
    /platform:anycpu `
    /target:exe `
    "/out:$resolvedOutput" `
    $sourcePath

if ($LASTEXITCODE -ne 0) {
    throw "C# compilation failed with exit code $LASTEXITCODE."
}

Get-Item -LiteralPath $resolvedOutput
