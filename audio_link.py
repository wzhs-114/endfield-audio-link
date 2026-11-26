"""Mirror Endfield's Windows session volume to its verified Qt audio service.

Only Windows Core Audio session settings are changed. No game-file writes,
memory reads, injected code, registry changes, or automatic startup installation.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import time
from typing import Any

BASE = Path(__file__).resolve().parent
RUNTIME = BASE / ".runtime"


@dataclass(frozen=True)
class Level:
    volume: float
    muted: bool

    def __post_init__(self):
        if not math.isfinite(self.volume) or not 0 <= self.volume <= 1:
            raise ValueError("volume must be finite and in [0, 1]")

    def close(self, other: Level) -> bool:
        return abs(self.volume - other.volume) < 0.00001 and self.muted == other.muted


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    name: str
    exe: str
    created: float
    args: tuple[str, ...] = ()


def normal_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def process_roles(processes: list[Process], game_exe: str | None = None):
    """Fail closed: require the observed game -> platform -> Qt audio chain.

    A role is (game PID, game creation time, 'game' or 'web'). Creation times
    protect against a stale parent PID being reused by a newer process.
    """
    roles = {}
    for game in processes:
        if game.name.lower() != "endfield.exe" or not game.exe:
            continue
        if Path(game.exe).name.lower() != "endfield.exe":
            continue
        if game_exe and normal_path(game.exe) != normal_path(game_exe):
            continue
        directory = normal_path(str(Path(game.exe).parent))
        group = (game.pid, game.created)
        roles[game.pid] = (*group, "game")
        for platform in processes:
            if (platform.ppid != game.pid or platform.created < game.created
                    or platform.name.lower() != "platformprocess.exe"
                    or normal_path(platform.exe) != normal_path(str(Path(directory) / "PlatformProcess.exe"))):
                continue
            for child in processes:
                if (child.ppid == platform.pid and child.created >= platform.created
                        and child.name.lower() == "qtwebengineprocess.exe"
                        and normal_path(child.exe) == normal_path(str(Path(directory) / "QtWebEngineProcess.exe"))
                        and "--type=utility" in child.args
                        and "--utility-sub-type=audio.mojom.AudioService" in child.args):
                    roles[child.pid] = (*group, "web")
    return roles


@dataclass
class Session:
    key: str
    device: str
    device_name: str
    pid: int
    created: float
    group: tuple[int, float]
    role: str
    active: bool
    control: Any

    def read(self) -> Level:
        return Level(float(self.control.GetMasterVolume()), bool(self.control.GetMute()))

    def write(self, level: Level):
        # Mute before changing volume; set volume before unmuting. This avoids
        # briefly unmuting a previously louder target during synchronization.
        if level.muted:
            self.control.SetMute(1, None)
        self.control.SetMasterVolume(level.volume, None)
        self.control.SetMute(int(level.muted), None)

    def describe(self):
        return {"key": self.key, "device": self.device, "device_name": self.device_name,
                "pid": self.pid, "created": self.created, "group": list(self.group), "role": self.role}


def choose_source(target: Session, sessions: list[Session]) -> Session | None:
    sources = [s for s in sessions if s.role == "game" and s.group == target.group]
    local = [s for s in sources if s.device == target.device]
    candidates = local or sources
    if len(candidates) == 1:
        return candidates[0]
    # Prefer a uniquely active session, but never choose arbitrarily when the
    # game exposes multiple active sources on the same output device.
    active = [s for s in candidates if s.active]
    return active[0] if len(active) == 1 else None


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.entries = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("entries"), dict):
                raise ValueError("Unsupported or damaged restore journal; preserve it before retrying")
            self.entries = data["entries"]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "updated_at": datetime.now().astimezone().isoformat(), "entries": self.entries}
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)


class Linker:
    def __init__(self, journal: Journal, logger=None):
        self.journal = journal
        self.log = logger or logging.getLogger("audio_link")
        self.changes = 0
        self.owned = set()

    def sync(self, target: Session, desired: Level) -> bool:
        current = target.read()
        if current.close(desired):
            return False
        record = self.journal.entries.get(target.key)
        externally_changed = (record and target.key not in self.owned
                              and not current.close(Level(**record["last"]))
                              and not current.close(Level(**record["intended"])))
        if (not record or record.get("status") == "restored"
                or record.get("created") != target.created or externally_changed):
            record = {**target.describe(), "before": asdict(current)}
            self.journal.entries[target.key] = record
        self.owned.add(target.key)
        # Write the recovery intention before touching Core Audio.
        record.update(status="pending", last=asdict(current), intended=asdict(desired))
        self.journal.save()
        try:
            target.write(desired)
            observed = target.read()
            record.update(last=asdict(observed), status="linked")
            self.journal.save()
            if not observed.close(desired):
                raise RuntimeError("Session did not retain requested level")
        except Exception:
            # Preserve the actual partial result so a normal shutdown can restore it.
            try:
                record.update(last=asdict(target.read()), status="write_failed")
                self.journal.save()
            except Exception:
                self.log.exception("Could not record partial write for pid=%s", target.pid)
            raise
        self.changes += 1
        self.log.info("SYNC pid=%s device=%s volume=%.1f%% mute=%s", target.pid,
                      target.device_name, desired.volume * 100, desired.muted)
        return True

    def tick(self, sessions: list[Session]):
        for target in sessions:
            if target.role != "web":
                continue
            source = choose_source(target, sessions)
            if source is None:
                continue
            self.sync(target, source.read())

    def restore(self, sessions: list[Session]):
        results = []
        live = {s.key: s for s in sessions if s.role == "web"}
        for key, record in self.journal.entries.items():
            if record.get("status") == "restored":
                continue
            target = live.get(key)
            outcome = {"pid": record["pid"], "key": key}
            try:
                if target is None or target.created != record["created"]:
                    outcome["result"] = "session_gone_or_identity_changed"
                else:
                    current = target.read()
                    last = Level(**record["last"])
                    intended = Level(**record["intended"])
                    # Do not overwrite an independent manual change made after
                    # the linker stopped. Interrupted partial writes can require
                    # manual review; they are deliberately not guessed at.
                    if not current.close(last) and not current.close(intended):
                        outcome["result"] = "skipped_external_change"
                    else:
                        before = Level(**record["before"])
                        target.write(before)
                        if not target.read().close(before):
                            raise RuntimeError("Restore readback mismatch")
                        record["status"] = "restored"
                        self.journal.save()
                        outcome["result"] = "restored"
            except Exception as exc:
                outcome.update(result="error", error=str(exc))
            self.log.info("RESTORE %s", json.dumps(outcome, ensure_ascii=False))
            results.append(outcome)
        return results


class WindowsBackend:
    def __init__(self, game_exe=None):
        if sys.platform != "win32":
            raise RuntimeError("This tool requires Windows")
        # Select MTA before importing comtypes; polling needs no UI message loop.
        sys.coinit_flags = 0
        import psutil
        from pycaw.pycaw import AudioUtilities, IAudioSessionControl2
        from pycaw.utils import AudioSession
        self.psutil, self.audio = psutil, AudioUtilities
        self.ctl_type, self.session_type = IAudioSessionControl2, AudioSession
        self.game_exe = game_exe
        self.roles = {}
        self.processes = {}
        self.sessions = []
        self.devices = []
        self.last_device_scan = -100
        self.last_errors = []

    def scan(self) -> list[Session]:
        self.last_errors = []
        processes = []
        for p in self.psutil.process_iter(["pid", "ppid", "name"]):
            if (p.info["name"] or "").lower() not in {
                    "endfield.exe", "platformprocess.exe", "qtwebengineprocess.exe"}:
                continue
            try:
                args = tuple(p.cmdline()) if p.info["name"].lower() == "qtwebengineprocess.exe" else ()
                processes.append(Process(p.pid, p.ppid(), p.name(), p.exe(), p.create_time(), args))
            except (self.psutil.AccessDenied, self.psutil.NoSuchProcess) as exc:
                self.last_errors.append(f"Cannot verify process {p.pid}: {type(exc).__name__}")
        self.roles = process_roles(processes, self.game_exe)
        self.processes = {p.pid: p for p in processes}
        # Refuse to write logs/state underneath any detected game directory.
        for p in processes:
            if self.roles.get(p.pid, (None, None, None))[2] == "game":
                if RUNTIME.is_relative_to(Path(p.exe).parent.resolve()):
                    raise RuntimeError("Move this project outside the game installation directory")
        now = time.monotonic()
        if not self.devices or now - self.last_device_scan >= 5:
            self.devices = self.audio.GetAllDevices(data_flow=0, device_state=1)
            self.last_device_scan = now
        sessions = []
        for device in self.devices:
            try:
                enum = device.AudioSessionManager.GetSessionEnumerator()
                for i in range(enum.GetCount()):
                    ctl = enum.GetSession(i).QueryInterface(self.ctl_type)
                    audio = self.session_type(ctl)
                    pid = audio.ProcessId
                    if pid not in self.roles or audio.State == 2:
                        continue
                    process = self.processes[pid]
                    # Recheck PID lifetime before accepting a newly enumerated handle.
                    live = self.psutil.Process(pid)
                    if live.create_time() != process.created:
                        continue
                    game_pid, game_created, role = self.roles[pid]
                    sessions.append(Session(
                        key=device.id + "|" + audio.InstanceIdentifier,
                        device=device.id, device_name=device.FriendlyName,
                        pid=pid, created=process.created, group=(game_pid, game_created),
                        role=role, active=audio.State == 1, control=audio.SimpleAudioVolume))
            except Exception as exc:
                self.last_errors.append(f"Device {device.FriendlyName}: {type(exc).__name__}: {exc}")
                self.last_device_scan = -100
        self.sessions = sessions
        return sessions

    def snapshot(self):
        sessions = self.scan()
        return {"time": datetime.now().astimezone().isoformat(),
                "processes": [{"pid": p.pid, "ppid": p.ppid, "name": p.name, "exe": p.exe,
                               "created": p.created, "role": self.roles[p.pid][2]}
                              for p in self.processes.values() if p.pid in self.roles],
                "sessions": [{**s.describe(), "active": s.active, **asdict(s.read())} for s in sessions],
                "warnings": self.last_errors}


class SingleInstance:
    def __enter__(self):
        import msvcrt
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.file = (RUNTIME / "link.lock").open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another linker/restore process is running in this project") from exc
        return self

    def __exit__(self, *_):
        self.file.close()


def make_logger():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("audio_link")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), RotatingFileHandler(
            RUNTIME / "audio-link.log", maxBytes=512_000, backupCount=2, encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number < 0.1:
        raise argparse.ArgumentTypeError("must be a finite number >= 0.1")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Print verified sessions without changing audio")
    mode.add_argument("--once", action="store_true", help="Synchronize once and leave the result in place")
    mode.add_argument("--restore", action="store_true", help="Restore live targets from the saved journal")
    parser.add_argument("--game-exe", help="Optional exact Endfield.exe path to restrict matching")
    parser.add_argument("--interval", type=positive, default=0.25, help="Level polling interval, default .25 seconds")
    parser.add_argument("--scan-interval", type=positive, default=2.0, help="Session discovery interval, default 2 seconds")
    parser.add_argument("--duration", type=positive, help="Stop after this many seconds (diagnostic use)")
    parser.add_argument("--leave-on-exit", action="store_true", help="Leave linked levels in place when monitoring ends")
    args = parser.parse_args(argv)
    if args.game_exe and not Path(args.game_exe).is_file():
        parser.error("--game-exe must point to an existing executable")
    backend = WindowsBackend(args.game_exe)
    if args.status:
        print(json.dumps(backend.snapshot(), ensure_ascii=False, indent=2))
        return 0
    # Validate placement before creating any project-local runtime files.
    sessions = backend.scan()
    with SingleInstance():
        logger = make_logger()
        journal = Journal(RUNTIME / "restore.json")
        linker = Linker(journal, logger)
        if args.restore:
            results = linker.restore(sessions)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return 1 if any(x["result"] == "error" for x in results) else 0
        logger.info("START source=Windows Endfield session; in-game slider is not read; Ctrl+C restores targets")
        if args.once:
            linker.tick(sessions)
            targets = [s for s in sessions if s.role == "web"]
            linked = sum(choose_source(s, sessions) is not None for s in targets)
            logger.info("ONCE matched_targets=%s eligible_targets=%s writes=%s", len(targets), linked, linker.changes)
            return 0 if linked else 2
        start = time.monotonic()
        next_scan = start + args.scan_interval
        previous_inventory = None
        failed = False
        try:
            while args.duration is None or time.monotonic() - start < args.duration:
                if time.monotonic() >= next_scan:
                    try:
                        sessions = backend.scan()
                    except Exception:
                        sessions = []
                        logger.exception("Discovery temporarily failed; waiting before retry")
                    next_scan = time.monotonic() + args.scan_interval
                inventory = tuple(sorted((s.key, s.role) for s in sessions))
                if inventory != previous_inventory:
                    logger.info("DISCOVER game_sessions=%s web_sessions=%s eligible_web=%s warnings=%s",
                                sum(s.role == "game" for s in sessions), sum(s.role == "web" for s in sessions),
                                sum(s.role == "web" and choose_source(s, sessions) is not None for s in sessions),
                                backend.last_errors)
                    previous_inventory = inventory
                try:
                    linker.tick(sessions)
                except Exception:
                    logger.exception("Session synchronization failed; rediscovering")
                    sessions = []
                    next_scan = 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("STOP requested")
        except Exception:
            failed = True
            logger.exception("Monitor stopped after an error")
        finally:
            if not args.leave_on_exit:
                try:
                    results = linker.restore(backend.scan())
                    if any(x["result"] == "error" for x in results):
                        failed = True
                except Exception:
                    failed = True
                    logger.exception("Automatic restore failed; use --restore after resolving the error")
            logger.info("STOP writes=%s restore=%s", linker.changes, not args.leave_on_exit)
        return int(failed)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
