<#
.SYNOPSIS
    Zero-dependency Windows build & auto-update script for EchoGrab located in the 'script' subfolder.
    Bundles portable Python, Tcl/Tk, FFmpeg, FFprobe, Deno (JS runtime for yt-dlp-ejs), and all
    yt-dlp[default] + shazamio dependencies into a single standalone apps\EchoGrab.exe binary.
#>

param(
    [switch]$CheckAndRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# --- Directory & Path Resolution ---
$ScriptDir     = $PSScriptRoot
$RootDir       = Split-Path -Parent $ScriptDir
$AppsDir       = Join-Path $RootDir "apps"
$AppScript     = Join-Path $ScriptDir "shazam_ytdlp_studio.py"
$ExeName       = "EchoGrab"
$PythonVersion = "3.11.9"

$BuildDir      = Join-Path $RootDir "build_env"
$PythonDir     = Join-Path $BuildDir "python_local"
$ToolsDir      = Join-Path $PythonDir "tools"
$PythonExe     = Join-Path $ToolsDir "python.exe"
$FfmpegDir     = Join-Path $BuildDir "ffmpeg_local"
$DenoDir       = Join-Path $BuildDir "deno_local"
$DistDir       = $AppsDir  # Output final .exe directly into the 'apps' directory
$FinalExe      = Join-Path $DistDir "$ExeName.exe"

# Ensure 'apps' and 'build_env' directories exist
New-Item -ItemType Directory -Force -Path $AppsDir | Out-Null
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

# -------------------------------------------------------------------------
# Helper: Compare Installed Local Packages vs Latest PyPI Releases
# -------------------------------------------------------------------------
function Test-PackageUpdatesAvailable {
    param(
        [string]$PyExe,
        [string[]]$Packages
    )
    if (-not (Test-Path $PyExe)) {
        Write-Host "[UPDATE CHECK] Local Python build environment not found. Full build required." -ForegroundColor Yellow
        return $true
    }

    $CheckPyCode = @"
import json, sys, urllib.request, importlib.metadata

pkgs = sys.argv[1:]
needs_update = False

for pkg in pkgs:
    try:
        local_ver = importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        print(f"[UPDATE] '{pkg}' is not installed locally yet.")
        needs_update = True
        continue

    try:
        url = f"https://pypi.org/pypi/{pkg}/json"
        req = urllib.request.Request(url, headers={"User-Agent": "EchoGrab-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            remote_ver = data["info"]["version"]
        if local_ver != remote_ver:
            print(f"[UPDATE] {pkg}: local {local_ver} -> latest {remote_ver}")
            needs_update = True
        else:
            print(f"[OK]     {pkg} is up to date ({local_ver})")
    except Exception as e:
        print(f"[SKIP]   Could not query PyPI for {pkg} ({e}); keeping {local_ver}")

sys.exit(10 if needs_update else 0)
"@

    & $PyExe -c $CheckPyCode @Packages
    return ($LASTEXITCODE -eq 10)
}

# -------------------------------------------------------------------------
# CheckAndRun Mode: Check for updates when launched via run.bat
# -------------------------------------------------------------------------
$WatchedPackages = @("shazamio", "yt-dlp", "yt-dlp-ejs")

if ($CheckAndRun -and (Test-Path $FinalExe)) {
    Write-Host "Checking for updates to shazamio, yt-dlp, and yt-dlp-ejs..." -ForegroundColor Cyan
    $UpdateNeeded = Test-PackageUpdatesAvailable -PyExe $PythonExe -Packages $WatchedPackages

    if (-not $UpdateNeeded) {
        Write-Host "All core packages are up to date. Launching $ExeName.exe..." -ForegroundColor Green
        Start-Process -FilePath $FinalExe -WorkingDirectory $AppsDir
        exit 0
    }

    Write-Host "`nPackage updates found! Removing old binary and rebuilding $ExeName.exe..." -ForegroundColor Yellow
    Get-Process -Name $ExeName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    Remove-Item -Path $FinalExe -Force -ErrorAction SilentlyContinue
}

# Locate app.ico (check apps\ first, then Root, then script\, and ensure a copy is in apps\)
$TargetIconPath = Join-Path $AppsDir "app.ico"
$IconPath = $null

if (Test-Path $TargetIconPath) {
    $IconPath = $TargetIconPath
} elseif (Test-Path (Join-Path $RootDir "app.ico")) {
    Copy-Item -Path (Join-Path $RootDir "app.ico") -Destination $TargetIconPath -Force
    $IconPath = $TargetIconPath
} elseif (Test-Path (Join-Path $ScriptDir "app.ico")) {
    Copy-Item -Path (Join-Path $ScriptDir "app.ico") -Destination $TargetIconPath -Force
    $IconPath = $TargetIconPath
}

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  Building Standalone Windows Binary: $ExeName.exe" -ForegroundColor Cyan
Write-Host "  Apps Directory : $AppsDir" -ForegroundColor Cyan
Write-Host "  Script Path    : $AppScript" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan

if (-not (Test-Path $AppScript)) {
    Write-Error "Could not find '$AppScript'. Ensure shazam_ytdlp_studio.py is inside the 'script' folder."
    exit 1
}

if (-not $IconPath) {
    Write-Warning "Could not find 'app.ico' in '$AppsDir', '$RootDir', or '$ScriptDir'. Building with default icon."
} else {
    Write-Host "Using Application Icon: $IconPath" -ForegroundColor Green
}

# -------------------------------------------------------------------------
# STEP 1: Download Local Portable Python
# -------------------------------------------------------------------------
if (-not (Test-Path $PythonExe)) {
    Write-Host "`n[1/5] Downloading portable Python $PythonVersion (x64)..." -ForegroundColor Yellow
    $NugetUrl = "https://www.nuget.org/api/v2/package/python/$PythonVersion"
    $NugetZip = Join-Path $BuildDir "python.nupkg.zip"

    Invoke-WebRequest -Uri $NugetUrl -OutFile $NugetZip
    Write-Host "      Extracting local Python environment..."
    Expand-Archive -Path $NugetZip -DestinationPath $PythonDir -Force
    Remove-Item $NugetZip -Force
} else {
    Write-Host "`n[1/5] Local Python already present at $PythonExe" -ForegroundColor Green
}

# -------------------------------------------------------------------------
# STEP 2: Download & Inject Official Tkinter / Tcl-Tk into Local Python
# -------------------------------------------------------------------------
$TkinterPyd = Join-Path $ToolsDir "DLLs\_tkinter.pyd"

if (-not (Test-Path $TkinterPyd)) {
    Write-Host "`n[2/5] Downloading & injecting official Tkinter (Tcl/Tk) from python.org..." -ForegroundColor Yellow
    $MsiTemp = Join-Path $BuildDir "tcltk_msi"
    New-Item -ItemType Directory -Force -Path $MsiTemp | Out-Null

    $BaseUrl = "https://www.python.org/ftp/python/$PythonVersion/amd64"
    $TclMsi  = Join-Path $MsiTemp "tcltk.msi"

    Invoke-WebRequest -Uri "$BaseUrl/tcltk.msi" -OutFile $TclMsi

    Write-Host "      Extracting Tcl/Tk libraries (_tkinter.pyd, tcl86t.dll, tk86t.dll, Lib\tkinter, tcl)..."
    $ExtractDir = Join-Path $MsiTemp "extracted"
    Start-Process "msiexec.exe" -ArgumentList "/a `"$TclMsi`" /qn TARGETDIR=`"$ExtractDir`"" -Wait -NoNewWindow

    New-Item -ItemType Directory -Force -Path (Join-Path $ToolsDir "DLLs") | Out-Null
    Copy-Item -Path (Join-Path $ExtractDir "DLLs\*") -Destination (Join-Path $ToolsDir "DLLs") -Recurse -Force
    Copy-Item -Path (Join-Path $ExtractDir "DLLs\*.dll") -Destination $ToolsDir -Force
    Copy-Item -Path (Join-Path $ExtractDir "Lib\tkinter") -Destination (Join-Path $ToolsDir "Lib\tkinter") -Recurse -Force
    Copy-Item -Path (Join-Path $ExtractDir "tcl") -Destination (Join-Path $ToolsDir "tcl") -Recurse -Force

    Remove-Item $MsiTemp -Recurse -Force
    & $PythonExe -c "import tkinter; print('      Tkinter verified! Tcl/Tk version:', tkinter.TkVersion)"
} else {
    Write-Host "`n[2/5] Tkinter (Tcl/Tk) already present in local Python." -ForegroundColor Green
}

# Bootstrap pip if not present
$PipExe = Join-Path $ToolsDir "Scripts\pip.exe"
if (-not (Test-Path $PipExe)) {
    Write-Host "      Bootstrapping pip into local Python..." -ForegroundColor Yellow
    $GetPip = Join-Path $BuildDir "get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip
    & $PythonExe $GetPip --no-warn-script-location
    Remove-Item $GetPip -Force
}

# -------------------------------------------------------------------------
# STEP 3: Download Standalone FFmpeg, FFprobe & Deno (JS Runtime for yt-dlp-ejs)
# -------------------------------------------------------------------------
$FfmpegExe  = Join-Path $FfmpegDir "ffmpeg.exe"
$FfprobeExe = Join-Path $FfmpegDir "ffprobe.exe"
$DenoExe    = Join-Path $DenoDir "deno.exe"

if (-not (Test-Path $FfmpegExe)) {
    Write-Host "`n[3/5] Downloading standalone FFmpeg & FFprobe binaries..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null

    $FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $FfmpegZip = Join-Path $BuildDir "ffmpeg.zip"
    $ExtractTemp = Join-Path $BuildDir "ffmpeg_extract"

    Invoke-WebRequest -Uri $FfmpegUrl -OutFile $FfmpegZip
    Expand-Archive -Path $FfmpegZip -DestinationPath $ExtractTemp -Force

    $BinFolder = Get-ChildItem -Path $ExtractTemp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    Copy-Item -Path $BinFolder.FullName -Destination $FfmpegExe -Force
    Copy-Item -Path (Join-Path $BinFolder.DirectoryName "ffprobe.exe") -Destination $FfprobeExe -Force

    Remove-Item $FfmpegZip -Force
    Remove-Item $ExtractTemp -Recurse -Force
} else {
    Write-Host "`n[3/5] Standalone FFmpeg & FFprobe binaries already present." -ForegroundColor Green
}

if (-not (Test-Path $DenoExe)) {
    Write-Host "      Downloading standalone Deno JS runtime (required by yt-dlp-ejs)..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path $DenoDir | Out-Null

    $DenoUrl = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
    $DenoZip = Join-Path $BuildDir "deno.zip"

    Invoke-WebRequest -Uri $DenoUrl -OutFile $DenoZip
    Expand-Archive -Path $DenoZip -DestinationPath $DenoDir -Force
    Remove-Item $DenoZip -Force
    Write-Host "      Standalone Deno runtime ready at $DenoExe" -ForegroundColor Green
} else {
    Write-Host "      Standalone Deno JS runtime already present." -ForegroundColor Green
}

# -------------------------------------------------------------------------
# STEP 4: Install / Upgrade Python Dependencies (including yt-dlp[default] & yt-dlp-ejs)
# -------------------------------------------------------------------------
Write-Host "`n[4/5] Installing/Updating packages in local Python..." -ForegroundColor Yellow
& $PythonExe -m pip install --upgrade pip setuptools wheel --no-warn-script-location
& $PythonExe -m pip install --upgrade `
    shazamio `
    "yt-dlp[default,curl-cffi]" `
    yt-dlp-ejs `
    sounddevice `
    scipy `
    numpy `
    pillow `
    requests `
    mutagen `
    sv-ttk `
    pyinstaller `
    --no-warn-script-location

# -------------------------------------------------------------------------
# STEP 5: Compile Standalone Executable into 'apps' Directory
# -------------------------------------------------------------------------
Write-Host "`n[5/5] Compiling standalone binary ($ExeName.exe) into 'apps' folder..." -ForegroundColor Yellow

$env:TCL_LIBRARY = Join-Path $ToolsDir "tcl\tcl8.6"
$env:TK_LIBRARY  = Join-Path $ToolsDir "tcl\tk8.6"

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", $ExeName,
    # Bundle FFmpeg, FFprobe, and Deno JS runtime binaries into _MEIPASS root (automatically on PATH)
    "--add-binary", "$FfmpegExe;.",
    "--add-binary", "$FfprobeExe;.",
    "--add-binary", "$DenoExe;.",
    # Explicitly bundle Tkinter extension, DLLs, and Tcl/Tk runtime folders
    "--add-binary", "$ToolsDir\DLLs\_tkinter.pyd;.",
    "--add-binary", "$ToolsDir\DLLs\tcl86t.dll;.",
    "--add-binary", "$ToolsDir\DLLs\tk86t.dll;.",
    "--add-data", "$ToolsDir\tcl\tcl8.6;_tcl_data",
    "--add-data", "$ToolsDir\tcl\tk8.6;_tk_data",
    "--collect-all", "tkinter",
    "--collect-all", "sv_ttk",
    "--hidden-import", "_tkinter",
    "--hidden-import", "tkinter",
    "--hidden-import", "PIL._tkinter_finder",
    # Collect native audio, Rust, yt-dlp, yt-dlp-ejs, and networking libraries
    "--collect-all", "sounddevice",
    "--collect-all", "_sounddevice_data",
    "--collect-all", "shazamio",
    "--collect-all", "shazamio_core",
    "--collect-all", "yt_dlp",
    "--collect-all", "yt_dlp_ejs",
    "--collect-all", "curl_cffi",
    "--collect-all", "certifi",
    "--distpath", $DistDir,
    "--workpath", (Join-Path $BuildDir "pyinstaller_work"),
    "--specpath", $BuildDir
)

# Attach app.ico as both the .exe icon and bundled runtime window icon if present
if ($IconPath) {
    $PyInstallerArgs += @("--icon", $IconPath)
    $PyInstallerArgs += @("--add-data", "$IconPath;.")
}

$PyInstallerArgs += $AppScript

& $PythonExe @PyInstallerArgs

if (Test-Path $FinalExe) {
    Write-Host "`n======================================================" -ForegroundColor Green
    Write-Host "  BUILD SUCCESSFUL!" -ForegroundColor Green
    Write-Host "  Executable Output: $FinalExe" -ForegroundColor Green
    Write-Host "======================================================" -ForegroundColor Green

    if ($CheckAndRun) {
        Write-Host "Launching $ExeName.exe..." -ForegroundColor Cyan
        Start-Process -FilePath $FinalExe -WorkingDirectory $AppsDir
    }
} else {
    Write-Error "Compilation failed. Check the PyInstaller output above for details."
    exit 1
}