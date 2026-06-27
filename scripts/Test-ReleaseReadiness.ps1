$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ForbiddenTrackedPatterns = @(
    "^data/",
    "^build/",
    "^dist/",
    "^design/",
    "__pycache__",
    "\.pfx$",
    "\.p12$",
    "\.key$",
    "\.pem$",
    "\.cer$",
    "todos\.json$",
    "TodoApp\.exe$",
    "TodoAppSetup\.exe$"
)

Push-Location $ProjectRoot
try {
    git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Release readiness requires a git repository. Run: git init -b main"
    }
    $Tracked = git ls-files
    $Leaks = foreach ($File in $Tracked) {
        foreach ($Pattern in $ForbiddenTrackedPatterns) {
            if ($File -match $Pattern) {
                $File
                break
            }
        }
    }

    if ($Leaks) {
        throw "Release readiness failed. Remove forbidden tracked files:`n$($Leaks -join "`n")"
    }

    $Exe = Join-Path $ProjectRoot "dist\TodoApp.exe"
    if (Test-Path $Exe) {
        $Signature = Get-AuthenticodeSignature -FilePath $Exe
        Write-Host "TodoApp.exe signature: $($Signature.Status)"
    }

    $Setup = Join-Path $ProjectRoot "dist\TodoAppSetup.exe"
    if (Test-Path $Setup) {
        $Signature = Get-AuthenticodeSignature -FilePath $Setup
        Write-Host "TodoAppSetup.exe signature: $($Signature.Status)"
    }

    Write-Host "Release readiness checks passed."
}
finally {
    Pop-Location
}
