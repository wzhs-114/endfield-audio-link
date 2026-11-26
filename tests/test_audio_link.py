import json
from pathlib import Path
import tempfile
import unittest

from audio_link import (Journal, Level, Linker, Process, Session, choose_source,
                        positive, process_roles)


class Volume:
    def __init__(self, volume=.8, muted=False):
        self.volume, self.muted = volume, muted
        self.calls = []
        self.before_write = None
        self.fail_volume = False

    def GetMasterVolume(self):
        return self.volume

    def GetMute(self):
        return self.muted

    def SetMasterVolume(self, value, _):
        if self.before_write:
            self.before_write()
        if self.fail_volume:
            raise OSError("device disconnected")
        self.calls.append(("volume", value))
        self.volume = value

    def SetMute(self, value, _):
        if self.before_write:
            self.before_write()
        self.calls.append(("mute", bool(value)))
        self.muted = bool(value)


def session(role, device="headphones", pid=None, volume=.8, muted=False, key=None, active=True, group=(1, 10.0)):
    pid = pid or (1 if role == "game" else 3)
    return Session(key or f"{device}/{pid}", device, device, pid, float(pid),
                   group, role, active, Volume(volume, muted))


class IdentityTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "fake-game"
        self.game = Process(1, 0, "Endfield.exe", str(root / "Endfield.exe"), 10)
        self.platform = Process(2, 1, "PlatformProcess.exe", str(root / "PlatformProcess.exe"), 11)
        self.audio = Process(3, 2, "QtWebEngineProcess.exe", str(root / "QtWebEngineProcess.exe"), 12,
                             ("--type=utility", "--utility-sub-type=audio.mojom.AudioService"))

    def test_verified_chain(self):
        self.assertEqual(process_roles([self.game, self.platform, self.audio])[3], (1, 10, "web"))

    def test_other_qt_application_is_excluded(self):
        foreign = Process(4, 2, self.audio.name, str(Path.cwd() / "other-app" / self.audio.name), 12, self.audio.args)
        self.assertNotIn(4, process_roles([self.game, self.platform, foreign]))

    def test_renderer_is_excluded(self):
        renderer = Process(3, 2, self.audio.name, self.audio.exe, 12, ("--type=renderer",))
        self.assertNotIn(3, process_roles([self.game, self.platform, renderer]))

    def test_reused_parent_pid_is_excluded(self):
        newer_platform = Process(2, 1, self.platform.name, self.platform.exe, 15)
        self.assertNotIn(3, process_roles([self.game, newer_platform, self.audio]))

    def test_exact_game_path_filter(self):
        self.assertFalse(process_roles([self.game, self.platform, self.audio], "different/Endfield.exe"))

    def test_missing_platform_fails_closed(self):
        self.assertNotIn(3, process_roles([self.game, self.audio]))


class RoutingTests(unittest.TestCase):
    def test_prefer_same_device(self):
        target = session("web")
        local = session("game")
        remote = session("game", "speakers")
        self.assertIs(choose_source(target, [local, remote, target]), local)

    def test_unique_source_cross_device(self):
        target = session("web", "speakers")
        source = session("game")
        self.assertIs(choose_source(target, [source, target]), source)

    def test_ambiguous_sources_are_not_chosen(self):
        target = session("web")
        self.assertIsNone(choose_source(target, [session("game", key="a"), session("game", key="b")]))

    def test_uniquely_active_source(self):
        target = session("web")
        active = session("game", key="active")
        inactive = session("game", key="inactive", active=False)
        self.assertIs(choose_source(target, [active, inactive]), active)

    def test_other_game_instance_is_excluded(self):
        self.assertIsNone(choose_source(session("web"), [session("game", group=(99, 99.0))]))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name) / "restore.json")
        self.linker = Linker(self.journal)

    def test_volume_and_mute_are_separate(self):
        target = session("web")
        self.linker.sync(target, Level(.2, False))
        self.assertEqual(target.read(), Level(.2, False))
        self.linker.sync(target, Level(.2, True))
        self.assertEqual(target.read(), Level(.2, True))

    def test_zero_volume_does_not_require_mute_flag(self):
        target = session("web")
        self.linker.sync(target, Level(0, False))
        self.assertEqual(target.read(), Level(0, False))

    def test_already_matching_session_has_no_writes(self):
        target = session("web")
        self.assertFalse(self.linker.sync(target, Level(.8, False)))
        self.assertFalse(target.control.calls)
        self.assertFalse(self.journal.path.exists())

    def test_restore_record_is_on_disk_before_first_write(self):
        target = session("web")
        def assert_saved():
            data = json.loads(self.journal.path.read_text(encoding="utf-8"))
            self.assertEqual(data["entries"][target.key]["before"], {"volume": .8, "muted": False})
        target.control.before_write = assert_saved
        self.linker.sync(target, Level(.2, True))

    def test_mute_precedes_volume_and_unmute_follows_volume(self):
        target = session("web")
        self.linker.sync(target, Level(.2, True))
        self.assertEqual(target.control.calls[0], ("mute", True))
        target.control.calls.clear()
        self.linker.sync(target, Level(.1, False))
        self.assertEqual(target.control.calls, [("volume", .1), ("mute", False)])

    def test_initial_state_survives_multiple_updates_and_reload(self):
        target = session("web", volume=1, muted=True)
        self.linker.sync(target, Level(.2, False))
        self.linker.sync(target, Level(.5, False))
        restarted = Linker(Journal(self.journal.path))
        result = restarted.restore([target])
        self.assertEqual(result[0]["result"], "restored")
        self.assertEqual(target.read(), Level(1, True))

    def test_manual_change_after_stop_is_not_overwritten_by_restore(self):
        target = session("web")
        self.linker.sync(target, Level(.2, False))
        target.control.volume = .6
        self.assertEqual(self.linker.restore([target])[0]["result"], "skipped_external_change")
        self.assertEqual(target.read(), Level(.6, False))

    def test_session_recreation_does_not_restore_to_wrong_instance(self):
        target = session("web")
        self.linker.sync(target, Level(.2, False))
        replacement = session("web", key="new-instance")
        self.assertEqual(self.linker.restore([replacement])[0]["result"], "session_gone_or_identity_changed")
        self.assertEqual(replacement.read(), Level(.8, False))

    def test_process_reuse_does_not_restore(self):
        target = session("web")
        self.linker.sync(target, Level(.2, False))
        target.created += 100
        self.assertEqual(self.linker.restore([target])[0]["result"], "session_gone_or_identity_changed")

    def test_new_takeover_respects_manual_state_after_previous_stop(self):
        target = session("web")
        self.linker.sync(target, Level(.2, False))
        target.control.volume = .6
        restarted = Linker(Journal(self.journal.path))
        restarted.sync(target, Level(.1, True))
        restarted.restore([target])
        self.assertEqual(target.read(), Level(.6, False))

    def test_partial_write_failure_can_be_restored(self):
        target = session("web")
        target.control.fail_volume = True
        with self.assertRaises(OSError):
            self.linker.sync(target, Level(.2, True))
        target.control.fail_volume = False
        self.assertEqual(self.linker.restore([target])[0]["result"], "restored")
        self.assertEqual(target.read(), Level(.8, False))

    def test_tick_does_not_change_source(self):
        source, target = session("game", volume=.3), session("web")
        self.linker.tick([source, target])
        self.assertEqual(target.read(), source.read())
        self.assertFalse(source.control.calls)

    def test_tick_waits_without_a_source(self):
        target = session("web")
        self.linker.tick([target])
        self.assertFalse(target.control.calls)

    def test_new_session_is_linked_on_next_inventory(self):
        source = session("game", volume=.1, muted=True)
        self.linker.tick([source])
        target = session("web", key="created-later")
        self.linker.tick([source, target])
        self.assertEqual(target.read(), source.read())

    def test_restore_does_not_touch_non_web_session(self):
        target = session("web")
        self.linker.sync(target, Level(.2, False))
        impostor = session("game", key=target.key, pid=target.pid, volume=.2)
        self.linker.restore([impostor])
        self.assertFalse(impostor.control.calls)

    def test_corrupt_journal_is_rejected(self):
        self.journal.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            Journal(self.journal.path)

    def test_invalid_levels_and_intervals(self):
        for value in (-1, 2, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Level(value, False)
        for value in ("0", "nan", "inf", "-1"):
            with self.subTest(value=value), self.assertRaises(Exception):
                positive(value)


if __name__ == "__main__":
    unittest.main()
