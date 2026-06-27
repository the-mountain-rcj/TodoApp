param(
    [string]$Version = "1.0.0",
    [switch]$SkipExeBuild
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SetupScript = Join-Path $ProjectRoot "installer\TodoApp.iss"
$SetupPath = Join-Path $ProjectRoot "dist\TodoAppSetup.exe"

function Find-InnoSetup {
    $Command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }

    $Candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            return $Candidate
        }
    }

    throw "Inno Setup was not found. Install it with: winget install JRSoftware.InnoSetup"
}

function Get-SigningCertificate {
    $Thumbprint = $env:TODOAPP_SIGN_CERT_THUMBPRINT
    if ($Thumbprint) {
        return Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Thumbprint -eq $Thumbprint } | Select-Object -First 1
    }

    return Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -eq "CN=TodoApp Local Code Signing" } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1
}

Push-Location $ProjectRoot
try {
    if (-not $SkipExeBuild) {
        & "$ProjectRoot\build.ps1"
    }

    $Iscc = Find-InnoSetup
    & $Iscc "/DMyAppVersion=$Version" $SetupScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }

    $Cert = Get-SigningCertificate
    if ($Cert) {
        $TimestampServer = if ($env:TODOAPP_TIMESTAMP_SERVER) { $env:TODOAPP_TIMESTAMP_SERVER } else { "http://timestamp.digicert.com" }
        $Signature = Set-AuthenticodeSignature -FilePath $SetupPath -Certificate $Cert -HashAlgorithm SHA256 -TimestampServer $TimestampServer
        if ($Signature.Status -ne "Valid") {
            throw "TodoApp installer signing failed: $($Signature.Status) $($Signature.StatusMessage)"
        }
    }
    else {
        Write-Warning "No code signing certificate found. The installer was built unsigned."
    }

    $Hash = Get-FileHash -Algorithm SHA256 -Path $SetupPath
    "$($Hash.Hash)  TodoAppSetup.exe" | Set-Content -Path "$SetupPath.sha256" -Encoding ascii
    Write-Host "Installer ready: $SetupPath"
    Write-Host "SHA256: $($Hash.Hash)"
}
finally {
    Pop-Location
}
