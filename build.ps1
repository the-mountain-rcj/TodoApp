$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundledPython = "C:\Users\rcj91\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Python = $env:PYTHON
if (-not $Python) {
    if (Test-Path $BundledPython) {
        $Python = $BundledPython
    }
}
if (-not $Python) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $Python = $PythonCommand.Source
    }
}

if (-not $Python -or -not (Test-Path $Python)) {
    throw "Python was not found. Install Python 3.12+ or set the PYTHON environment variable."
}

Push-Location $ProjectRoot
try {
    $RequiredPackages = @(
        @{ ImportName = "PyInstaller"; PackageName = "pyinstaller" },
        @{ ImportName = "webview"; PackageName = "pywebview" }
    )

    foreach ($Package in $RequiredPackages) {
        & $Python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$($Package.ImportName)') else 1)"
        if ($LASTEXITCODE -ne 0) {
            & $Python -m pip install $Package.PackageName
        }
    }

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --icon "$ProjectRoot\assets\icons\todoapp.ico" `
        --name TodoApp `
        --distpath "$ProjectRoot\dist" `
        --workpath "$ProjectRoot\build" `
        --specpath "$ProjectRoot" `
        --collect-all webview `
        --collect-all pythonnet `
        --collect-all clr_loader `
        --add-data "$ProjectRoot\assets;assets" `
        "$ProjectRoot\app_webview.py"

    $CertSubject = "CN=TodoApp Local Code Signing"
    $CertThumbprint = $env:TODOAPP_SIGN_CERT_THUMBPRINT
    if ($CertThumbprint) {
        $Cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Thumbprint -eq $CertThumbprint } | Select-Object -First 1
    }
    else {
        $Cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq $CertSubject } | Sort-Object NotAfter -Descending | Select-Object -First 1
    }

    if ($Cert) {
        $ExePath = Join-Path $ProjectRoot "dist\TodoApp.exe"
        $TimestampServer = if ($env:TODOAPP_TIMESTAMP_SERVER) { $env:TODOAPP_TIMESTAMP_SERVER } else { "http://timestamp.digicert.com" }
        $Signature = Set-AuthenticodeSignature -FilePath $ExePath -Certificate $Cert -HashAlgorithm SHA256 -TimestampServer $TimestampServer
        if ($Signature.Status -ne "Valid") {
            throw "TodoApp signing failed: $($Signature.Status) $($Signature.StatusMessage)"
        }
    }
    else {
        Write-Warning "No code signing certificate found. The exe was built unsigned."
    }
}
finally {
    Pop-Location
}
