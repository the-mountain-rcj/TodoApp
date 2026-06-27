# Release Guide

## 1. Prepare the repository

```powershell
git init -b main
git add .
git commit -m "Prepare TodoApp for public release"
```

Create the GitHub repository under `the-mountain-rcj`, then connect it:

```powershell
git remote add origin https://github.com/the-mountain-rcj/TodoApp.git
git push -u origin main
```

If GitHub CLI is installed and authenticated:

```powershell
gh repo create the-mountain-rcj/TodoApp --public --source . --remote origin --push
```

## 2. Build locally

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

## 3. Build installer

Install Inno Setup:

```powershell
winget install JRSoftware.InnoSetup
```

Build the setup file:

```powershell
powershell -ExecutionPolicy Bypass -File .\build-installer.ps1 -Version 1.0.0
```

## 4. Sign release files

Use a trusted signing certificate or signing service before public distribution. Sign:

- `dist\TodoApp.exe`
- `dist\TodoAppSetup.exe`

The local self-signed development certificate should not be used for public releases.

## 5. Publish a GitHub Release

```powershell
git tag v1.0.0
git push origin main --tags
```

The GitHub Actions workflow builds unsigned CI artifacts by default. Upload signed release assets from your local machine, or set the repository secret `TODOAPP_UPLOAD_RELEASE=true` only after trusted signing is configured in CI.

## 6. Pre-publish check

Before pushing, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Test-ReleaseReadiness.ps1
```

This checks that private data, certificates, and build outputs are not tracked.
