# Builds the Windows download: DMM-Downloader.exe, DMM-Fetch.exe, the license and README.txt, as a zip
# (dist\DMM-Downloader-<version>-windows-x64.zip) and in the program folder dist\DMM-Downloader\.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 [-Python <python.exe>] [-AlsoRefuse <word>,...]
#
# The build is made in build\windows\dist\DMM-Downloader\ (PyInstaller empties that folder) and zipped from
# there. In dist\DMM-Downloader\ only the build's own files are replaced (install-build.ps1): downloads,
# jav-it.exe, DECRYPT.MOD, settings and anything else put there stay.
# Never included: FFmpeg, MediaInfo (users download them, README-windows.txt says where) and jav-it with its
# modules (from jav-it's developer; users put them next to the .exe).
# The build refuses to finish when a file names this computer (user name, folders, the GitHub account) or holds a
# word listed in packaging\windows\refused-words.txt (not in git) or DMM_DOWNLOADER_REFUSE: check_dist.py.
param(
    [string]$Python = "python",
    [string[]]$AlsoRefuse = @()
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$build = Join-Path $root "build\windows"
$venv = Join-Path $build "venv"
$stage = Join-Path $build "dist"
$built = Join-Path $stage "DMM-Downloader"  # the new build (PyInstaller's own output folder)
$dist = Join-Path $root "dist"
$app = Join-Path $dist "DMM-Downloader"  # the program folder, which may hold downloads, jav-it, ...

$version = (Select-String -Path (Join-Path $root "dmm_downloader\__init__.py") -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "DMM Downloader $version"

if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Making the build environment ($build\venv)..."
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
}
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $root "requirements.txt") -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$running = Get-Process -Name "DMM-Downloader", "DMM-Fetch" -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$app\*" }
if ($running) { throw "DMM-Downloader is running from $app (Quit it on its page, or close its window), then build again." }
# PyInstaller deletes its output folder first: that is the staging folder under build\, never dist\DMM-Downloader.
& $py -m PyInstaller --noconfirm --clean --log-level WARN --distpath $stage --workpath (Join-Path $build "work") (Join-Path $PSScriptRoot "dmm-downloader.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# The license and the short guide (FFmpeg, MediaInfo, jav-it, VPN split tunnelling).
Copy-Item (Join-Path $root "LICENSE") (Join-Path $built "LICENSE.txt")
Copy-Item (Join-Path $PSScriptRoot "README-windows.txt") (Join-Path $built "README.txt")

# Other people's programs are never part of the download.
$never = @("ffmpeg.exe", "ffprobe.exe", "MediaInfo.exe", "jav-it.exe", "DECRYPT.MOD", "VR.MOD")
$bundled = @(Get-ChildItem -Recurse -File $built | Where-Object { $_.Name -in $never -or $_.Extension -eq ".MOD" })
if ($bundled) { throw "The build contains $(($bundled | ForEach-Object { $_.Name }) -join ', '): never included." }
foreach ($exe in @("DMM-Downloader.exe", "DMM-Fetch.exe")) {
    if (-not (Test-Path (Join-Path $built $exe))) { throw "$exe is missing from the build." }
}

# Nothing in the download may name this computer or the person who built it.
$refuse = @($env:USERNAME, $env:COMPUTERNAME, $root, (Split-Path $root -Parent), $env:USERPROFILE) + $AlsoRefuse
$gitUser = (git -C $root config user.name) 2>$null
$gitMail = (git -C $root config user.email) 2>$null
if ($gitUser) { $refuse += $gitUser }
if ($gitMail) { $refuse += $gitMail }
& $py (Join-Path $PSScriptRoot "check_dist.py") $built @($refuse | Where-Object { $_ })
if ($LASTEXITCODE -ne 0) { throw "The build names this computer or a refused name (see above): not zipped." }

# The zip holds only the build (never the program folder's downloads, jav-it or modules).
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$zip = Join-Path $dist "DMM-Downloader-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $built -DestinationPath $zip -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)

# The program folder: only the build's own files are replaced.
& (Join-Path $PSScriptRoot "install-build.ps1") -From $built -To $app

Write-Host ""
Write-Host "Done: $zip ($size MB)"
Write-Host "SHA-256: $hash"
