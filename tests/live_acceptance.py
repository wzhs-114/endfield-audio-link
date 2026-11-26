r"""Opt-in real audio test. Briefly changes Endfield session settings, then restores.

Run: .venv\Scripts\python.exe tests\live_acceptance.py --confirm-audio-changes
Keep the game announcement page open. Do not change volume during this test.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audio_link import WindowsBackend, choose_source, Level


def wait_for(predicate, timeout):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return round(time.monotonic() - start, 3)
        time.sleep(.025)
    raise TimeoutError("Condition did not become true within timeout")


def main():
    if sys.argv[1:] != ["--confirm-audio-changes"]:
        raise SystemExit("Explicit --confirm-audio-changes is required")
    sys.stdout.reconfigure(encoding="utf-8")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    backend = WindowsBackend()
    sessions = backend.scan()
    targets = [s for s in sessions if s.role == "web" and choose_source(s, sessions)]
    if len(targets) != 1:
        raise SystemExit(f"Expected exactly one verified web session, found {len(targets)}; open announcement first")
    target = targets[0]
    source = choose_source(target, sessions)
    before_source, before_target = source.read(), target.read()
    result = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "source": source.describe(), "target": target.describe(),
              "before_source": vars(before_source), "before_target": vars(before_target), "checks": []}
    child = None
    output_path = runtime / "live-monitor-output.log"
    print("LIVE TEST: volume/mute sequence starting; all settings will be restored", flush=True)
    try:
        with output_path.open("w", encoding="utf-8") as output:
            child = subprocess.Popen(
                [sys.executable, str(ROOT / "audio_link.py"), "--duration", "16", "--interval", ".1", "--scan-interval", "1"],
                cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            def ready():
                if child.poll() is not None:
                    raise RuntimeError(f"Monitor exited early with {child.returncode}; inspect {output_path}")
                return "DISCOVER" in output_path.read_text(encoding="utf-8")
            startup = wait_for(ready, 25)
            result["monitor_startup_seconds"] = startup
            # All chosen live levels are <=20%; 100% is covered with simulated volumes.
            cases = [("zero_without_mute", Level(0, False)),
                     ("volume_15_percent", Level(.15, False)),
                     ("mute_at_15_percent", Level(.15, True)),
                     ("unmute_at_15_percent", Level(.15, False)),
                     ("volume_20_percent", Level(.2, False)),
                     ("mute_at_20_percent", Level(.2, True))]
            for name, desired in cases:
                source.write(desired)
                latency = wait_for(lambda: target.read().close(desired), 3)
                unchanged = source.read().close(desired)
                result["checks"].append({"name": name, "expected": vars(desired),
                                         "actual": vars(target.read()), "latency_seconds": latency,
                                         "source_not_overwritten": unchanged, "passed": unchanged})
                if not unchanged:
                    raise AssertionError("Monitor unexpectedly changed source")
                time.sleep(.3)
            child.wait(timeout=25)
            result["monitor_exit_code"] = child.returncode
            restored = target.read().close(before_target)
            result["checks"].append({"name": "automatic_target_restore_after_duration",
                                     "passed": restored, "actual": vars(target.read())})
            if child.returncode or not restored:
                raise AssertionError("Monitor did not exit and restore correctly")
            result["passed"] = True
    except Exception as exc:
        result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        source.write(before_source)
        # Restore the test fixture even if the subprocess failed; this is distinct
        # from the monitor's own automatic-restore assertion recorded above.
        if not target.read().close(before_target):
            target.write(before_target)
            result["fixture_cleanup_required"] = True
        result["source_restored"] = source.read().close(before_source)
        result["target_restored"] = target.read().close(before_target)
        path = runtime / "live-acceptance.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("Saved:", path)
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
