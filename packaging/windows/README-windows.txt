DMM Downloader for Windows
==========================

Downloads the DMM videos your account can download (DMM Premium, DMM's VR Channel or the videos you bought)
and decrypts them with jav-it: 2D videos into .mkv, VR videos into .mp4 (with VR.MOD; without
it they are saved as DMM sends them, .wsdcf).

Before the first start
----------------------
1. jav-it: jav-it.exe, DECRYPT.MOD (2D videos) and VR.MOD (VR videos) come from jav-it's
   developer (Patreon). Put them next to DMM-Downloader.exe. Without jav-it and DECRYPT.MOD
   2D videos can't be downloaded (the page refuses to start them); without VR.MOD, VR videos
   are saved encrypted (.wsdcf) instead of decrypted.
2. MediaInfo, the command-line version (CLI): https://mediaarea.net/en/MediaInfo/Download
   Choose Windows, then the "CLI" download (zip). It holds MediaInfo.exe.
3. FFmpeg (only needed to join parts into one file): https://www.gyan.dev/ffmpeg/builds/
   Take the "release essentials" build (zip). ffmpeg.exe and ffprobe.exe are in its bin folder.
   (FFmpeg for other systems: https://ffmpeg.org/download.html)

Put MediaInfo.exe, ffmpeg.exe and ffprobe.exe next to DMM-Downloader.exe (or in a folder
named "bin" there). Or keep them anywhere on PATH, or give their places on the page:
Settings -> Programs -> "Use other copies". They are NOT included in this download.

Start
-----
Start DMM-Downloader.exe. A window opens and your browser shows the downloader's page
(only this computer can use it). Keep the window open while you download; closing it stops
the downloader and every program it started (DMM-Fetch, jav-it, FFmpeg, MediaInfo).

First time, in the page's Settings tab: log in to DMM ("Log in and save"). The password is kept
in Windows Credential Manager, never in a file. jav-it decrypts with this same login, so you set
it only here: "Log out" removes it, logging in with another account replaces it. If DMM doesn't
accept a correct login, it wants an extra check (captcha): log in once at dmm.co.jp in your
browser, then try again. jav-it keeps its own sign-in in this folder (auth.json; removed on
"Log out" and when you change account): never give this folder to someone else with it inside.

Then, on the Download tab: paste content ids or DMM links (or pick videos in "My library"),
then "Download". Up to 4 videos download at the same time (Settings), VR videos one at a time.

Using a VPN (split tunneling)
-----------------------------
DMM refuses its download links when they are asked from a VPN address, but its file servers
don't, and the files can come much faster through a VPN. So the tool has two programs:

- DMM-Downloader.exe: logs in, reads your library and asks DMM for the download links.
  It must NOT go through the VPN.
- DMM-Fetch.exe: downloads only the files themselves. It may go through the VPN.

In your VPN app's split tunneling, either exclude DMM-Downloader.exe from the VPN, or let
only DMM-Fetch.exe use the VPN. Examples for Proton VPN: Settings -> Split tunneling ->
"Standard" mode: add DMM-Downloader.exe (excluded from the VPN); or "Inverse" mode: add
DMM-Fetch.exe (only it uses the VPN). Then restart DMM-Downloader.exe.
Without a VPN nothing needs to be set: both programs use your normal connection.

If a download fails with "Is a VPN on?", DMM-Downloader.exe is still going through the VPN.

Files
-----
Videos are saved in the "downloads" folder next to the program unless you choose another one
(Download tab -> "Save videos in"). The log (without passwords, cookies or download links) is in
%LOCALAPPDATA%\DMMDownloader\Logs, the settings in %APPDATA%\DMMDownloader\config.ini.

Terminal use: DMM-Downloader.exe --help (e.g. DMM-Downloader.exe get sone00876).

Windows may warn that the program is from an unknown publisher (it isn't signed):
"More info" -> "Run anyway".
With Windows' Smart App Control on, the program is blocked completely (there is no
"Run anyway"). Then use the Python version of the tool instead: run.bat in its source code
(its README says what it needs).
