"""Terminal interface: dmm-downloader [get] CID... | gui | rates | login | logout | doctor | config.

Without arguments (a double-click on run.bat or the .exe) it starts the browser page (web.py)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__, children, config, credentials, ids, links, logs, pipeline
from . import decrypt as decrypting
from .dmm import DmmError, DmmSession, JapanRoute, LoginError
from .merging import tools

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_SETUP, EXIT_INTERRUPTED = 0, 1, 2, 130
COMMANDS = {"get", "gui", "rates", "login", "logout", "doctor", "config"}
JAVIT_NAMES = ("jav-it",)


class SetupError(Exception):
    """Something must be installed or configured first."""


def size_text(size: float) -> str:
    return f"{size / 1024**3:.2f} GB" if size >= 1024**3 else f"{size / 1024**2:.0f} MB"


class ConsoleReport(pipeline.Report):
    """Steps as lines; a progress bar redrawn in place (every 10% when output goes to a file)."""

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty()
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.start = time.monotonic()
        self.first_done: float | None = None
        self.last_draw = 0.0
        self.last_step = -1
        self.drawn = False

    def step(self, text: str) -> None:
        print(f"    {text}", flush=True)

    def progress(self, done: float, total: float | None, unit: str = "bytes") -> None:
        with self.lock:
            now = time.monotonic()
            if self.first_done is None:
                self.first_done, self.start = done, now
            fraction = done / total if total else None
            if self.tty:
                if now - self.last_draw < 0.5 and fraction != 1:
                    return
                self.last_draw, self.drawn = now, True
                if unit == "fraction":
                    text = f"{fraction * 100:5.1f}%"
                else:
                    speed = (done - self.first_done) / max(0.001, now - self.start)
                    text = f"{size_text(done)}" + (f" / {size_text(total)}" if total else "") + f"  {speed / 1024**2:.1f} MB/s"
                bar = "#" * int((fraction or 0) * 30)
                print(f"\r    [{bar:.<30}] {'' if unit == 'fraction' or fraction is None else f'{fraction * 100:5.1f}% '}{text}   ", end="", flush=True)
            elif fraction is not None and int(fraction * 10) > self.last_step:
                self.last_step = int(fraction * 10)
                print(f"    {self.last_step * 10}%", flush=True)

    def end_progress(self) -> None:
        if self.drawn:
            print()
        self.reset()


# -- setup -----------------------------------------------------------------------------------


def find_javit(settings: config.Settings) -> Path:
    try:
        javit = tools.find_tool("jav-it", settings.jav_it or None)
    except tools.ToolError:
        raise SetupError(
            "jav-it was not found. Put jav-it (and DECRYPT.MOD, for VR also VR.MOD) next to dmm-downloader, or set "
            f"jav_it in the settings file ({config.config_path()})."
        ) from None
    if not (javit.parent / decrypting.DECRYPT_MODULE).exists():
        raise SetupError(f"DECRYPT.MOD was not found next to jav-it ({javit.parent}). jav-it needs it to decrypt DMM videos.")
    return javit


def find_mediainfo(settings: config.Settings) -> Path:
    try:
        return tools.find_tool("mediainfo", settings.mediainfo or None)
    except tools.ToolError:
        raise SetupError(
            "MediaInfo (command-line version) was not found. Windows: download the CLI zip from "
            "https://mediaarea.net/en/MediaInfo/Download/Windows and put mediainfo.exe next to dmm-downloader; "
            "Linux: sudo apt install mediainfo."
        ) from None


def find_merge_tools(settings: config.Settings) -> tools.Tools | None:
    try:
        return tools.find_tools(settings.ffmpeg or None, settings.ffprobe or None)
    except tools.ToolError:
        return None


def find_tools_to_get(settings: config.Settings) -> tuple[pipeline.Tools, Path | None]:
    """The programs for `get`, and jav-it when it was found. As on the page, a missing one fails only the videos that
    need it: 2D videos need jav-it with DECRYPT.MOD, and MediaInfo; VR videos are decrypted with jav-it's VR.MOD and
    MediaInfo, else saved encrypted (no program needed)."""
    missing = []
    try:
        javit = find_javit(settings)
    except SetupError as exc:
        missing.append(str(exc))
        try:
            javit = tools.find_tool("jav-it", settings.jav_it or None)  # without DECRYPT.MOD: VR.MOD may be there
        except tools.ToolError:
            javit = None
    try:
        mediainfo_path = find_mediainfo(settings)
    except SetupError as exc:
        missing.append(str(exc))
        mediainfo_path = None
    found = pipeline.Tools(javit or Path("jav-it"), mediainfo_path or Path("mediainfo"), find_merge_tools(settings),
                           no_vr=decrypting.vr_problem(javit, mediainfo_path), no_2d=" ".join(missing))
    return found, javit


def open_session(settings: config.Settings, interactive: bool) -> tuple[DmmSession, credentials.Login]:
    """A logged-in DMM session and its login, which jav-it decrypts with: no session without a login."""
    login = credentials.find(interactive=False)
    if not login and interactive:
        login = credentials.ask()
        if login:
            offer_save(login)
    if not login:
        raise SetupError("No DMM login. Run `dmm-downloader login` once, or set DMM_USERNAME and DMM_PASSWORD.")
    session = DmmSession(settings.user_agent, settings.retries)
    session.japan = JapanRoute(settings.vrchannel_route)
    saved = config.session_path()
    if session.load(saved, login.username) and session.is_logged_in():  # another account's session isn't used
        log.info("using the saved DMM session")
        return session, login
    print("Logging in to DMM...")
    session.login(login.username, login.password)
    session.save(saved)
    return session, login


def japan_text(reaches: bool | None, why: str) -> str:
    """The Japan route check (links.japan_check) in words."""
    if reaches:
        return "reaches Japan"
    if reaches is False:
        return f"not in Japan ({why}); set vrchannel_route ({config.config_path()}) to a proxy in Japan"
    return f"couldn't be checked ({why})"


def offer_save(login: credentials.Login) -> None:
    if login.source != "typed" or not credentials.store_available() or not sys.stdin.isatty():
        return
    if input("Save this login in your system's password store for next time? [y/N] ").strip().lower() in ("y", "yes"):
        print("Saved." if credentials.save(login) else "Could not save it.")


# -- commands --------------------------------------------------------------------------------


def read_ids(args: argparse.Namespace) -> list[str]:
    lines = list(args.ids or [])
    if args.ids_file:
        try:
            lines += args.ids_file.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError as exc:
            raise SetupError(f"can't read {args.ids_file}: {exc}") from exc
    found, invalid = ids.parse_lines(lines)
    for entry in invalid:
        print(f"Not a DMM content id, skipped: {entry}  (use the content id like abc00123, not the DVD id ABC-123)")
    return found


def cmd_get(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    cids = read_ids(args)
    if not cids:
        print("No content ids given.")
        return EXIT_FAILED
    tool_paths, javit = find_tools_to_get(settings)
    if tool_paths.no_2d:
        print(f"Note: 2D videos can't be decrypted here, they fail: {tool_paths.no_2d}")
        print("VR videos are " + (f"saved encrypted (.wsdcf): {tool_paths.no_vr}." if tool_paths.no_vr else
                                  "decrypted (VR.MOD found next to jav-it)."))
    merge = settings.merge and not args.no_merge
    if merge and tool_paths.merge is None:
        print("Note: FFmpeg not found; videos in several H.264 parts will stay in parts.")
    out_dir = (args.out or (Path(settings.output_dir) if settings.output_dir else config.default_output_dir())).expanduser().resolve()
    from .web import fetch_helper_command

    helper = fetch_helper_command(settings)
    if helper:
        print(f"Files are downloaded by {Path(helper[0]).name} (only that step can go through a VPN).")
    opts = pipeline.Options(
        out_dir=out_dir, source=args.source or settings.source, rate=args.rate, merge=merge,
        keep_dcv=args.keep_dcv, overwrite=args.overwrite, retries=settings.retries, javit_email=settings.javit_email,
        fetch_helper=helper,
    )
    session, login = open_session(settings, interactive)
    decrypting.match_javit_login(javit, login, config.config_dir())  # another account: jav-it's sign-in goes
    report = ConsoleReport()
    results = []
    try:
        print(f"Saving to {out_dir}")
        for index, cid in enumerate(cids, 1):
            print(f"\n[{index}/{len(cids)}] {cid}")
            try:
                result = pipeline.process(cid, session, login, tool_paths, opts, report)
            except Exception as exc:  # never let one video stop the others (like the page): it goes to failed_ids.txt
                log.exception("%s: unexpected error", cid)
                result = pipeline.Result(cid, False, f"unexpected error: {logs.redact(exc)} (details in the log)")
            report.end_progress()
            results.append(result)
            if result.ok:
                print(f"    {'Already there' if result.skipped else 'Done'}: {result.message if not result.skipped else ', '.join(f.name for f in result.files)}")
            else:
                print(f"    FAILED: {result.message}")
            for warning in result.warnings:
                print(f"    note: {warning}")
    finally:
        session.close()

    failed = [r.cid for r in results if not r.ok]
    print(f"\nFinished: {sum(r.ok and not r.skipped for r in results)}  Already there: {sum(r.skipped for r in results)}  Failed: {len(failed)}")
    # failed_ids.txt keeps the ids still to do: this run's failures, and earlier ones this run didn't finish.
    failed_file = out_dir / "failed_ids.txt"
    done = {r.cid for r in results if r.ok}
    try:
        earlier = failed_file.read_text(encoding="utf-8").split() if failed_file.exists() else []
        left = list(dict.fromkeys([cid for cid in earlier if cid not in done] + failed))
        if left:
            out_dir.mkdir(parents=True, exist_ok=True)
            failed_file.write_text("\n".join(left) + "\n", encoding="utf-8")
        elif failed_file.exists():
            failed_file.unlink()
        if failed:
            print(f"Failed ids: {failed_file} (run again with -i to retry; finished parts are reused)")
    except OSError:
        pass
    if failed and tool_paths.no_2d and all(r.message == tool_paths.no_2d for r in results if not r.ok):
        return EXIT_SETUP  # only a missing program stopped them
    return EXIT_FAILED if failed else EXIT_OK



def cmd_gui(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    from . import web

    try:
        return web.serve(settings, port=args.port, open_browser=not args.no_browser)
    except web.PortError as exc:
        raise SetupError(str(exc)) from None


def cmd_rates(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    cids = read_ids(args)
    session, _ = open_session(settings, interactive)
    code = EXIT_OK
    try:
        for cid in cids:
            try:
                choice = links.find(session, cid, args.source or settings.source)
            except links.NoLinks as exc:
                print(f"{cid}: {exc}")
                code = EXIT_FAILED
                continue
            print(f"{cid} ({links.source_word(choice.source)}):")
            for label, parts in choice.options.items():
                mark = "  <- best" if label == choice.label else ""
                print(f"    {label}: {parts} part(s){mark}")
    finally:
        session.close()
    return code


def cmd_login(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    if credentials.from_environment():
        print("DMM_USERNAME and DMM_PASSWORD are set in the environment: that login is used (also by jav-it). "
              "Change or remove them there instead.")
        return EXIT_FAILED
    login = credentials.ask()
    if not login:
        print("No login given.")
        return EXIT_FAILED
    session = DmmSession(settings.user_agent, settings.retries)
    try:
        session.login(login.username, login.password)
        session.save(config.session_path())
    finally:
        session.close()
    print("Logged in to DMM.")
    if credentials.store_available():
        print("Login saved in your system's password store." if credentials.save(login) else "Could not save the login.")
    else:
        print("No system password store here: set DMM_USERNAME and DMM_PASSWORD for jav-it, or type them each time.")
    return EXIT_OK


def cmd_logout(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    removed = credentials.forget()
    try:
        javit = find_javit(settings)
    except SetupError:
        javit = None
    decrypting.forget_javit_login(javit, config.config_dir())  # jav-it's own sign-in (auth.json) too
    try:
        config.session_path().unlink()
        removed = True
    except OSError:
        pass
    print("Saved login and DMM session removed." if removed else "Nothing was saved.")
    if credentials.from_environment():
        print("DMM_USERNAME and DMM_PASSWORD are still set in the environment: they are used until you remove them.")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    print(f"dmm-downloader {__version__}, Python {sys.version.split()[0]} on {sys.platform}")
    print(f"Settings: {config.config_path()}")
    print(f"Logs:     {config.log_dir()}")
    ok = True
    found = {}
    for name, finder in (("jav-it", find_javit), ("mediainfo", find_mediainfo)):
        try:
            found[name] = finder(settings)
            print(f"{name}: {found[name]}")
        except SetupError as exc:
            print(f"{name}: {exc}")
            ok = False
    no_vr = decrypting.vr_problem(found.get("jav-it"), found.get("mediainfo"))
    print("VR videos: " + (f"saved encrypted (.wsdcf), {no_vr}" if no_vr else "decrypted (VR.MOD found next to jav-it)"))
    merge_tools = find_merge_tools(settings)
    print(f"ffmpeg/ffprobe: {merge_tools.ffmpeg}, {merge_tools.ffprobe}" if merge_tools else
          "ffmpeg/ffprobe: not found (only needed to join H.264 parts)")
    login = credentials.find(interactive=False)
    print(f"DMM login: {'from ' + login.source if login else 'none saved (dmm-downloader login)'}")
    session = DmmSession(settings.user_agent, 2)
    session.japan = JapanRoute(settings.vrchannel_route)
    try:
        if session.load(config.session_path()):
            print("DMM session: " + ("logged in" if session.is_logged_in() else "expired (logs in again on the next download)"))
        else:
            print("DMM session: none yet")
        print(f"VR Channel downloads (DMM gives them only in Japan), Japan route: {session.japan.shown()}: "
              + japan_text(*links.japan_check(session)))
    finally:
        session.close()
    print("Everything needed was found." if ok else "Something is missing, see above.")
    return EXIT_OK if ok else EXIT_SETUP


def cmd_config(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    path = config.ensure_config_file()
    print(f"Settings file: {path}")
    print("Open it in a text editor to change the settings. Current values:")
    for key, value in vars(config.load(path)).items():
        print(f"    {key} = {value}")
    return EXIT_OK


# -- entry -----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dmm-downloader", description="Download and decrypt DMM videos you have access to.")
    parser.add_argument("--version", action="version", version=f"dmm-downloader {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the log while running")
    # -v also after the command and its options ("run.bat --source library -v abc00123"). SUPPRESS: when it isn't
    # repeated there, the value given before the command stays.
    verbose = argparse.ArgumentParser(add_help=False)
    verbose.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="show the log while running")
    sub = parser.add_subparsers(dest="command")

    def with_ids(p: argparse.ArgumentParser) -> None:
        p.add_argument("ids", nargs="*", help="content ids (abc00123) or DMM links")
        p.add_argument("-i", "--ids-file", type=Path, help="text file with content ids (one or more per line)")
        p.add_argument("--source", choices=links.SOURCES,
                       help="where to download from (default: auto = Premium, then the VR Channel, then your library)")

    g = sub.add_parser("get", parents=[verbose], help="download and decrypt videos (default command)")
    with_ids(g)
    g.add_argument("-o", "--out", type=Path, help="folder for the finished videos")
    g.add_argument("--rate", help="a quality from `rates` instead of the best one")
    g.add_argument("--no-merge", action="store_true", help="keep videos in several parts as separate files")
    g.add_argument("--keep-dcv", action="store_true",
                   help="keep the encrypted downloads next to the video (<id>.dcv; VR: <id>.wsdcf)")
    g.add_argument("--overwrite", action="store_true", help="download videos again that are already there")

    gui = sub.add_parser("gui", parents=[verbose], help="the browser page (the default without arguments)")
    gui.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: 8766, else a free one)")
    gui.add_argument("--no-browser", action="store_true", help="only show the address, don't open the browser")
    with_ids(sub.add_parser("rates", parents=[verbose], help="show the qualities DMM offers for videos"))
    sub.add_parser("login", parents=[verbose], help="log in to DMM and save the login")
    sub.add_parser("logout", parents=[verbose], help="remove the saved login and DMM session")
    sub.add_parser("doctor", parents=[verbose], help="check that everything needed is found")
    sub.add_parser("config", parents=[verbose], help="show (and create) the settings file")
    return parser


def owns_console() -> bool:
    """True when Windows opened a console just for us (double-click), which closes when we exit."""
    if os.name != "nt" or not sys.stdin.isatty():
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) <= 2
    except Exception:
        return False


COMMAND_FUNCTIONS = {"get": cmd_get, "gui": cmd_gui, "rates": cmd_rates, "login": cmd_login, "logout": cmd_logout,
                     "doctor": cmd_doctor, "config": cmd_config}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    pause = owns_console()
    interactive = sys.stdin.isatty()
    if not argv:
        argv = ["gui"]  # a double-click: the browser page
    # "dmm-downloader abc00123 -o D:\x" means "get": add the command after the global options.
    start = 0
    while start < len(argv) and argv[start] in ("-v", "--verbose"):
        start += 1
    if start < len(argv) and argv[start] not in COMMANDS and argv[start] not in ("-h", "--help", "--version"):
        argv.insert(start, "get")
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return EXIT_SETUP
    log_path = logs.setup(args.verbose)
    log.info("dmm-downloader %s: %s", __version__, args.command)
    settings = config.load()
    try:
        code = COMMAND_FUNCTIONS[args.command](args, settings, interactive)
    except SetupError as exc:
        print(exc)
        code = EXIT_SETUP
    except LoginError as exc:
        print(f"Login problem: {exc}")
        code = EXIT_SETUP
    except DmmError as exc:
        print(f"DMM problem: {exc}")
        code = EXIT_FAILED
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue where it stopped.")
        code = EXIT_INTERRUPTED
    finally:
        children.stop_all()  # no download program, jav-it or FFmpeg keeps running after the tool
    if log_path and code not in (EXIT_OK,):
        print(f"Log: {log_path}")
    if pause:
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code
