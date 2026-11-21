# DMM Downloader

Downloads DMM videos your account has access to and decrypts them with jav-it. The sources are
DMM Premium, DMM's VR Channel (the monthly VR plan) and your own library (bought videos). You get one checked `.mkv` per video (VR: `.mp4`), named
after its content id (`abc00123.mkv`), ready to watch.

- **Sources**: Premium first, then the VR Channel, then your library (`--source` to choose). The next
  source is also tried when one lists a video but doesn't give this account its download, or lacks the
  quality you asked for.
- **Quality**: the best available (4K first; VP9 before H.264 at the same quality), or one you
  pick with `--rate`.
- **VR videos** from the VR Channel or your library: downloaded in the best VR quality (8k, then uhq, hq, 12000,
  6000, 4000) and decrypted with jav-it's VR.MOD into `.mp4` (without VR.MOD: saved encrypted), see
  [VR videos](#vr-videos).
- **Resumes**: a stopped or failed run continues where it stopped. Finished parts are reused and a
  half-downloaded part is continued.
- **Checks every file**: size, MediaInfo errors, length, and the video id and part number DMM
  writes inside each file. A bad download, or a decrypted file that fails these checks, is
  downloaded again once.
- **Clean names**: DMM's own file names (`abc00123hhb1`, `abc000232d_v1_drm_a_4k1`, ...) become
  `abc00123.mkv`. Videos in several H.264 parts are joined into one file with a chapter per part.
  4K VP9 parts stay separate: `abc00123-pt1.mkv`, `abc00123-pt2.mkv`.
- **Browser page** (the default): a queue of videos, your DMM library to pick from, settings and the
  log, on your own computer only. Up to 4 videos at the same time (VR one at a time).
- **VPN split tunneling**: the file downloads can run in a separate program that alone goes through
  a VPN, see [Using a VPN](#using-a-vpn).
- **Windows program**: `DMM-Downloader.exe`, no Python needed, see [Windows program](#windows-program).

## What you need

- **Python 3.10 or newer**
  - Windows: [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
  - Linux: `python3` and `python3-venv`.
- **jav-it and DECRYPT.MOD**, from jav-it's developer (Patreon). Put both in this folder. For VR
  videos also **VR.MOD**, next to jav-it (without it VR videos are saved encrypted).
- **MediaInfo, command-line version**
  - Windows: the "CLI" zip from [mediaarea.net](https://mediaarea.net/en/MediaInfo/Download/Windows).
    Put `MediaInfo.exe` in this folder.
  - Linux: `sudo apt install mediainfo`.
- **FFmpeg** (only to join videos in several parts)
  - Windows: [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), `ffmpeg.exe` and `ffprobe.exe` in this folder.
  - Linux: `sudo apt install ffmpeg`.
- **A DMM account** with Premium, the VR Channel, or the videos in its library. DMM only serves Japan:
  outside Japan the tool presents itself as Googlebot, which DMM currently lets through.

## Start

Windows: double-click `run.bat` (or `DMM-Downloader.exe`, see [Windows program](#windows-program)).
Linux: `sh run.sh`. The first start prepares a private Python environment in `.venv` (one minute);
later starts are immediate.

Without anything after it, the tool opens its page in your browser: log in to DMM in its Settings
tab, then add content ids or DMM links (or pick videos in "My library") on the Download tab. The page
runs on `127.0.0.1` only, with a key in the first address, so no other computer or website can use
it. Keep the window open while downloading; closing it stops the tool and every program it started
(DMM-Fetch, jav-it, FFmpeg, MediaInfo), so nothing keeps writing in the background. Everything
below also works in the terminal.

Check that everything is found:

```
run.bat doctor
```

Log in once (asks for your DMM email and password):

```
run.bat login
```

The login is checked with DMM, and the session is saved so DMM doesn't see a new login every
time. Where it is kept:
- **Password**: your system's password store (Windows Credential Manager, macOS Keychain, Linux
  Secret Service), never a plain file.
- **Without a password store** (e.g. a Linux server): set `DMM_USERNAME` and `DMM_PASSWORD` in
  the environment, or type them each time.

jav-it decrypts with the same login, and you set it only here: jav-it has no login setting of its
own, and the downloader gives it the email and password (as `DMM_USERNAME` and `DMM_PASSWORD`, in
jav-it's environment only) each time it decrypts. "Log out" (`logout`) removes the saved login and the
DMM session; logging in with another account replaces them, and jav-it gets the new login from the
next video on. jav-it also keeps its own sign-in next to itself (`auth.json`): it is removed on log
out and before jav-it runs with another login, so jav-it signs in again with the new one. A DMM
session saved for another account than the login is not used (it logs in again).
With `DMM_USERNAME` and `DMM_PASSWORD` set, they are the login: change or remove them there.

## Download

```
run.bat abc00123 xyz00456
run.bat -i ids.txt -o D:\Videos
run.bat "https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=abc00123/"
run.bat --source vrchannel "https://www.dmm.co.jp/monthly/vr/-/detail/=/cid=abcd00123/"
```

You can also drag a `.txt` file of ids onto `run.bat`.

Use **content ids** (`abc00123`), not DVD ids (`ABC-123`). They are used exactly as written:
`sone096` and `sone00096` can be different videos. DMM links work too: the id is taken from their
`cid=abc00123` or `?id=abc00123` part.

| Option | What it does |
|---|---|
| `-i`, `--ids-file FILE` | Read ids from a text file (one or more per line, `#` starts a comment) |
| `-o`, `--out FOLDER` | Where finished videos go (default: `downloads` in the folder you start it from; the Windows program: next to `DMM-Downloader.exe`) |
| `--source auto\|premium\|vrchannel\|library` | Where to download from (default `auto`: Premium, then the VR Channel, then your library) |
| `--rate QUALITY` | A quality shown by `rates` instead of the best one |
| `--no-merge` | Keep videos in several parts as separate files |
| `--keep-dcv` | Keep the encrypted downloads, next to the video: `abc00123.dcv` (or `-pt1.dcv`, `-pt2.dcv`, ...; VR: `.wsdcf`) |
| `--overwrite` | Download again videos that are already there |
| `-v` | Show the log while running |

Other commands:

| Command | What it does |
|---|---|
| `gui` | The browser page (also without any command); `--port`, `--no-browser` |
| `rates abc00123` | Show the qualities DMM offers |
| `login` / `logout` | Save / remove your login and the DMM session |
| `doctor` | Check that everything needed is found |
| `config` | Show (and create) the settings file |

Videos that failed are listed in `failed_ids.txt` in the output folder. Run again with
`-i failed_ids.txt`: finished parts are not downloaded again.

While working, files live in `<output>\.dmm-downloader\<content id>\` (parts are joined there
too). That folder is removed when the video is finished. A `lock` file there keeps a second copy of
the tool (or a second row of the page) from working on the same video at the same time; a lock left
by a closed or crashed copy is taken over by itself.

A 2D video is named after the id DMM writes inside its file, which is
nearly always the id you typed; the tool says so when it differs, and remembers it in
`<output>\.dmm-downloader\ids.json`. A video already in the output folder, under the id you typed
or DMM's id for it, is not downloaded again; a video saved in parts (`-pt1`, `-pt2`, ...) counts
only when no run of it is unfinished (a stopped run may have saved some parts only). An existing
file is never replaced (only with `--overwrite` / "Download again videos already saved"): the new
download then stays in the work folder and the video fails with a message naming both files.

Without jav-it, DECRYPT.MOD or MediaInfo, 2D videos fail with a message saying what is missing,
and VR videos are still saved (encrypted, see below).

## VR videos

VR videos come from DMM's VR Channel (with a VR Channel subscription) or your library (bought
videos). DMM sends them encrypted (`.wsdcf`, in 8k about
3-4 GB per part); a part that comes as a web page or text instead of video data (an error page) is
refused and asked for once more.

**With VR.MOD next to jav-it** (and MediaInfo), each part is decrypted by jav-it
(`jav-it decrypt -i <part> -o <part.mp4> -t dmm-vr`) and checked: its size against the download,
MediaInfo's damage check, and the length when MediaInfo can read the encrypted file. VR parts are
never joined, whatever "Join parts" says: a video in one part becomes `sivr00457.mp4`, one in several
parts `sivr00457-pt1.mp4`, `sivr00457-pt2.mp4`, ..., numbered as DMM numbers the parts. VR files
keep the id you typed.
- A part whose decrypted file fails the checks is downloaded again, but only once per video (each
  VR download counts toward DMM's limit, below).
- jav-it gets 5 minutes plus 1 second per 5 MB of the part; then it is stopped and the video fails.
- If jav-it answers that it needs DMM VR Player's license (older jav-it versions did), the video
  fails at once with that message; the encrypted parts are kept for the next run.
- "Keep the encrypted downloads" (`--keep-dcv`) keeps `sivr00457.wsdcf` / `-pt1.wsdcf`, ... next to
  the result.
- Encrypted files saved earlier (`sivr00457.wsdcf`, `sivr00457-pt1.wsdcf`, ... in the output
  folder, e.g. by a version that didn't decrypt VR) don't count as "already saved": when the video
  is added again they are decrypted instead of downloaded (the page says so in the row), when they
  are exactly the parts of the quality chosen. Otherwise (another quality's parts, a part missing)
  they are not used, so encodes are never mixed: the parts are downloaded, and the files saved
  earlier are left as they are (a note says so). Without "Keep the encrypted downloads" the files
  used are removed after a checked decrypt.

**The VR Channel** (`--source vrchannel`, or "VR Channel only" on the page; `auto` asks it after
Premium) needs a [Japan route](#vr-channel-the-japan-route) for its downloads. The tool opens the
video's VR Channel page (`https://www.dmm.co.jp/monthly/vr/-/detail/=/cid=<id>/`) as it opens every
DMM page, which works from anywhere and tells:
- `not in DMM's VR Channel`: DMM has no VR Channel page for that id (2D videos never are). In
  `auto` this is only mentioned for VR videos.
- `your DMM account has no VR Channel subscription`: the video is in the channel, but the page
  offers to join the channel instead of the download.
- `in DMM's VR Channel from <date> (not yet)`: the channel offers it later.

For a subscriber, the tool then asks, through the Japan route and as a normal Windows browser, for the
page again (now with its qualities) and for its download list, the way the page itself does. The
channel's qualities are its own (for example UHQ, HQ, 12000, 6000 and 4000, often no 8k); the best one
is taken as for library VR, and the parts are downloaded and decrypted like a VR video from your
library (same names and checks). When the channel lists the video but DMM doesn't give this account
the download, `auto` tries your library next.

DMM's limit on how often one VR video is downloaded (below) was seen on library downloads; count on
it for VR Channel downloads too.

**Without VR.MOD** (or without jav-it or MediaInfo), VR videos are saved encrypted, as DMM sends
them: `sivr00457.wsdcf`, or `sivr00457-pt1.wsdcf`, `sivr00457-pt2.wsdcf`, ..., with a note saying
what is missing. Watch them in DMM VR Player.

A stopped download continues on the next run with the same `--rate` (another quality starts the
video over). DMM limits how often one VR video can be downloaded: after many downloads of the same
video (about 20 in a short time), DMM refuses it for a while (other videos still work). The tool
first checks that you are still logged in (if not, it stops at once with `log in again`), then
waits up to about 17 minutes and asks you to run again later; the parts already downloaded are
kept. A VPN on the main program gets the same refusal from DMM: keep it off the VPN.

## VR Channel: the Japan route

DMM gives the VR Channel's qualities, download list and download links only to a normal browser in
Japan: to a browser elsewhere it answers `not available in your region`, and to the name the tool
uses for everything else (Googlebot) it shows the page without them. So only these steps take a
route through Japan, set under Settings -> Downloads, "Japan route for the VR Channel" (the setting
`vrchannel_route`). Logging in, your library and Premium never use it.

- **A proxy in Japan** (the way to use): enter its address, such as `http://127.0.0.1:25345`. Nothing
  else on the computer changes. One way, with a VPN that gives WireGuard configs for its servers:
  1. Download your VPN's WireGuard config for one server in Japan. Its `Endpoint` must be that same
     server: a general name that points to several servers makes the server ignore the connection
     (in wireproxy's window, `Handshake did not complete` again and again).
  2. Get [wireproxy](https://github.com/pufferffish/wireproxy) (its Releases page;
     `wireproxy_windows_amd64.tar.gz` on Windows) and make a file `wireproxy.conf`:
     ```
     WGConfig = C:\path\to\your-japan-server.conf

     [http]
     BindAddress = 127.0.0.1:25345
     ```
  3. Start it (`wireproxy.exe -c wireproxy.conf`) and leave its window open while downloading
     from the VR Channel. It should say `Received handshake response`.

  Only http proxies work. A user name and password in the address (`http://user:pass@host:port`) are
  kept in the settings file as written (and hidden in the log and on the page's messages).
- **A VPN app connected to a server in Japan**, with the setting empty: the tool then uses its own
  connection, so the VPN app must carry DMM-Downloader. The VR Channel works that way, but Premium and
  library downloads fail, as DMM refuses its download links from VPN addresses (see
  [Using a VPN](#using-a-vpn)): use a proxy instead.

"Check" (and `run.bat doctor`) asks DMM, as a normal Windows browser through the route, for the VR
Channel's start page: `reaches Japan` or `not in Japan`. It doesn't log in and downloads nothing.

The download links go through the route too. The files themselves come over your normal connection
(or DMM-Fetch, below). If DMM's file server refuses a VR Channel file there (some file links work only
from the address that asked for them), the tool says so and fetches that part through the route
instead: with a proxy through the proxy, without one in the main program. That asks DMM for a new link,
which counts once more toward its VR download limit. Some VPN servers in Japan are refused by DMM for
the download links themselves: "Check" only shows that DMM shows the channel there.

## Using a VPN

DMM refuses its download links when they are asked from a VPN address (`DMM's download server
answered HTTP 404. Is a VPN on?`), but its file servers (`stcN.dmm.com`, VR `wsd-cN.dmm.com`) don't,
and through a VPN the files can come several times faster. So the last step, the file itself, can run
in its own program, `DMM-Fetch.exe` (next to `DMM-Downloader.exe` in the Windows program).
Logging in, the library and the download links stay in the main program; the file server address
reaches the separate program through a pipe, never on its command line.

In your VPN app's split tunneling, exclude `DMM-Downloader.exe` from the VPN, or let only
`DMM-Fetch.exe` use it (Proton VPN: Settings -> Split tunneling, "Standard" or "Inverse" mode).
Without a VPN nothing needs to be set.

The page shows which program downloads the files (Settings -> Downloads). The setting `fetch_helper`:
empty = `DMM-Fetch.exe` next to the program when it is there, `off` = always in the main program,
or the path of the program. From source, `module` runs it as `python -m dmm_downloader.fetch`; that
is the same `python.exe`, so split tunneling can't tell the two apart: use the Windows program for it.

## Settings

`run.bat config` shows the settings file:
- Windows: `%APPDATA%\DMMDownloader\config.ini`
- Linux: `~/.config/dmm-downloader/config.ini`

It holds the default output folder, the program locations, the source, merging, retries, how many
videos download at the same time (`parallel`, 1-8, default 4; VR always one), the download program
(`fetch_helper`, see [Using a VPN](#using-a-vpn)), the Japan route for the VR Channel
(`vrchannel_route`, see [VR Channel: the Japan route](#vr-channel-the-japan-route)) and your Patreon
email for jav-it, only if it differs from your DMM email (`javit_email`). The page's Settings tab changes
them too.

## Windows program

`powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1` builds the program
(`DMM-Downloader.exe`, `DMM-Fetch.exe`, `README.txt` from
`packaging\windows\README-windows.txt`) with PyInstaller in its own environment (`build\windows\venv`),
in `build\windows\dist\`, and makes the zip `dist\DMM-Downloader-<version>-windows-x64.zip` from
there. In `dist\DMM-Downloader\` it then replaces only its own files (the two programs,
`_internal`, `LICENSE.txt`, `README.txt`): downloads, `jav-it.exe`, `DECRYPT.MOD` and anything else
put there stay (`packaging\windows\install-build.ps1`). It never includes FFmpeg, MediaInfo, jav-it
or its modules (users get them themselves) and refuses to finish when a file names the computer it
was built on (user name, folders, git account: `packaging\windows\check_dist.py`), or holds a word
you list in `packaging\windows\refused-words.txt` (one per line, not in git) or in
`DMM_DOWNLOADER_REFUSE` (separated by `;`). The program isn't signed: Windows SmartScreen warns
("More info" -> "Run anyway"). With Windows' Smart App Control on, the program is blocked completely
(there is no "Run anyway"): use the Python version then (`run.bat`, see [Start](#start)).

The programs' icon, `packaging\windows\dmm-downloader.ico`, is the page's mark (a download sign on a
rounded tile, also `favicon.svg`), drawn by `python packaging\windows\make_icon.py` with Python alone.

## Problems

| Message | What to do |
|---|---|
| `DMM did not accept the login` | Check email and password. If they're right, DMM wants an extra check (captcha) for this login: log in once at dmm.co.jp in a browser on this computer, then try again. |
| `not on DMM Premium ...; not in your DMM library` | Your account can't download this video: it isn't in Premium and you haven't bought it (check the id: `sone096` and `sone00096` can be different videos). |
| `your DMM account has no VR Channel subscription` | The VR video is in DMM's VR Channel, but this account has no VR Channel plan. Subscribe on DMM, then look it up again, or buy the video (your library). |
| `not in DMM's VR Channel` | "VR Channel only" was chosen and the channel doesn't have this id. Choose "Premium, VR Channel, then my library" (`--source auto`) to look further. |
| `in DMM's VR Channel from <date> (not yet)` | The channel offers it from that day on: try again then. |
| `in DMM's VR Channel, but DMM gives its downloads only in Japan` | The Japan route doesn't reach Japan for DMM: set a Japan proxy (or check the one set), then look it up again. See [VR Channel: the Japan route](#vr-channel-the-japan-route). |
| `no answer through the Japan proxy (...): is it running and connected?` | Start the proxy (wireproxy: its window must stay open and show `Received handshake response`), then try again. |
| `DMM answered "not available in your region" to the Japan route` | The route stopped counting as Japan between the lookup and the download: check it ("Check"), then start the video again. |
| `... in your DMM library for streaming only` | You bought it as a streaming-only (or rental) purchase: DMM offers no download. |
| `jav-it asks for your Patreon email` | Set `javit_email` in the settings file. |
| `jav-it could not reach its server` | jav-it checks your membership online; try again later. |
| `jav-it says it needs DMM VR Player's license` | This jav-it can't decrypt VR without the player: update jav-it and VR.MOD. The encrypted parts are kept; start the video again afterwards. Or remove VR.MOD to have VR videos saved encrypted. |
| `jav-it took longer than ... and was stopped` | jav-it hung. Start the video again (nothing is downloaded again); the jav-it output named in the message may say why. |
| `the decrypted file is too small` / `damaged` | The tool already downloaded that part once more and decrypted it again. `... also after downloading it again`: try again later (jav-it or DMM's file may be at fault). |
| `DMM's download server answered HTTP 404. Is a VPN on?` | The main program goes through a VPN: see [Using a VPN](#using-a-vpn). |
| `not enough free space ...` / `disk full: ...` | Free some space on that drive, then start again: what was downloaded is kept. With several videos at once, the space the others still need counts too, so a video may wait for the next run. |
| `your DMM login has expired: log in again (Settings)` | DMM no longer sees the saved session as logged in. Page: Settings -> DMM account, "Log in and save", then "Look up again". Terminal: `run.bat login`, then run again. |

- **Log**:
  - Windows: `%LOCALAPPDATA%\DMMDownloader\Logs\dmm-downloader.log`
  - Linux: `~/.local/state/dmm-downloader/`
  
  Passwords, cookies and download links are removed from it.
- **Exit codes**: `0` all done, `1` some videos failed, `2` setup problem, `130` stopped.

## Changes in 1.0.1

- Settings changed quickly one after the other are all saved. Before, one of them could fail with
  `unexpected error: [WinError 5] Access is denied` and keep its old value.
- "DMM limit: later" rows open by themselves, like Failed and Not available rows, and keep their
  steps once finished, so the 2, 5 and 10 minute waits stay visible.
- The Japan route advice no longer contradicts the VPN advice: use a proxy in Japan. A VPN app in
  Japan on DMM-Downloader makes Premium and library downloads fail.
- Clearer texts: the jav-it Patreon email field says it is needed only when it differs from your DMM
  email, and the Already saved note no longer says it twice. US spelling throughout ("license",
  "split tunneling").
- The Windows program is blocked completely while Windows' Smart App Control is on: the READMEs now
  say so, and to use the Python version (`run.bat`) then.

## Development

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -t .
```

The tests use a fake jav-it, a local file server and small clips made with FFmpeg. The tests
that need FFmpeg and MediaInfo are skipped when those aren't installed.

`dmm_downloader/merging/` is a copy of the merge code of an earlier version of VideoMerger. It stays as it is: the downloader joins only its own DMM downloads, so VideoMerger's later changes (looser part names, WMV parts) aren't needed here.
