"""Guards for Windows application registration and the uninstaller.

Two things must never regress here:
  1. Auto-update keeps working: every shortcut and registry value points at
     the CANONICAL exe path the updater swaps in place, never at the volatile
     copy the user happened to launch (Downloads, a USB stick).
  2. The deferred uninstall cleanup runs `Remove-Item -Recurse -Force`, so the
     only paths that may reach it are our own two folders directly under
     %LOCALAPPDATA% / %APPDATA%.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_install


EXE = r"C:\Users\jo\AppData\Local\FTC Whisper\FTC Whisper.exe"


class ShortcutTests(unittest.TestCase):
    def test_start_menu_link_is_recreated_when_missing(self):
        state = {"exe": EXE, "desktop_shortcut": True}
        needed = app_install.shortcuts_needed(
            state, EXE, "start.lnk", "desk.lnk", exists=lambda p: False
        )
        self.assertIn("start.lnk", needed)

    def test_start_menu_link_is_retargeted_when_exe_path_changes(self):
        state = {"exe": r"C:\Downloads\FTC-Whisper.exe", "desktop_shortcut": True}
        needed = app_install.shortcuts_needed(
            state, EXE, "start.lnk", "desk.lnk", exists=lambda p: True
        )
        self.assertEqual(["start.lnk"], needed)

    def test_desktop_shortcut_is_never_recreated_once_made(self):
        # Putting back a shortcut the user deleted is adware behaviour.
        state = {"exe": EXE, "desktop_shortcut": True}
        needed = app_install.shortcuts_needed(
            state, EXE, "start.lnk", "desk.lnk", exists=lambda p: p == "start.lnk"
        )
        self.assertNotIn("desk.lnk", needed)

    def test_first_install_creates_both(self):
        needed = app_install.shortcuts_needed(
            {}, EXE, "start.lnk", "desk.lnk", exists=lambda p: False
        )
        self.assertEqual(["start.lnk", "desk.lnk"], needed)

    def test_steady_state_writes_nothing(self):
        state = {"exe": EXE, "desktop_shortcut": True}
        needed = app_install.shortcuts_needed(
            state, EXE, "start.lnk", "desk.lnk", exists=lambda p: True
        )
        self.assertEqual([], needed)

    def test_shortcut_script_targets_the_canonical_exe(self):
        script = app_install.shortcut_script(["a.lnk", "b.lnk"], EXE)
        self.assertIn(EXE, script)
        self.assertIn("$l.TargetPath = $t", script)
        # Icon comes from the exe itself, so it survives any loose .ico going
        # missing and matches the taskbar pin. Built with single quotes: a
        # double-quoted "$t,0" is stripped to the comma operator by
        # powershell.exe -Command and IShellLink then refuses to save.
        self.assertIn("$l.IconLocation = $t + ',0'", script)
        self.assertNotIn('"', script)
        self.assertIn("a.lnk", script)
        self.assertIn("b.lnk", script)

    def test_shortcut_script_escapes_quotes_in_paths(self):
        script = app_install.shortcut_script([r"C:\o'brien\x.lnk"], EXE)
        self.assertIn("o''brien", script)


class UninstallEntryTests(unittest.TestCase):
    def _values(self):
        return dict(
            (name, value)
            for name, _dword, value in app_install.uninstall_values(
                EXE, "1.6.46", os.path.dirname(EXE), 700_000, "20260805"
            )
        )

    def test_entry_has_what_installed_apps_shows(self):
        v = self._values()
        self.assertEqual("FTC Whisper", v["DisplayName"])
        self.assertEqual("1.6.46", v["DisplayVersion"])
        self.assertEqual("FTC Safety Solutions", v["Publisher"])
        self.assertEqual(f"{EXE},0", v["DisplayIcon"])
        self.assertEqual(700_000, v["EstimatedSize"])

    def test_uninstall_string_is_quoted_and_runnable(self):
        v = self._values()
        # The install path contains a space; an unquoted command runs
        # "C:\Users\jo\AppData\Local\FTC" with "Whisper\..." as an argument.
        self.assertEqual(f'"{EXE}" --uninstall', v["UninstallString"])
        self.assertEqual(f'"{EXE}" --uninstall /S', v["QuietUninstallString"])

    def test_version_fields_parse(self):
        v = self._values()
        self.assertEqual(1, v["VersionMajor"])
        self.assertEqual(6, v["VersionMinor"])

    def test_no_modify_or_repair_buttons(self):
        v = self._values()
        self.assertEqual(1, v["NoModify"])
        self.assertEqual(1, v["NoRepair"])


class DeleteGuardTests(unittest.TestCase):
    def setUp(self):
        self.local = os.environ.get("LOCALAPPDATA") or r"C:\Users\jo\AppData\Local"
        self.roaming = os.environ.get("APPDATA") or r"C:\Users\jo\AppData\Roaming"

    def test_accepts_our_own_folders(self):
        self.assertTrue(
            app_install.safe_to_delete(os.path.join(self.local, "FTC Whisper"))
        )
        self.assertTrue(
            app_install.safe_to_delete(os.path.join(self.roaming, "FTC Whisper"))
        )

    def test_rejects_everything_else(self):
        for path in (
            "",
            self.local,
            os.path.dirname(self.local),
            r"C:\Windows",
            r"C:\FTC Whisper",
            os.path.join(self.local, "FTC Whisper", "models"),
            os.path.join(self.local, "Microsoft"),
        ):
            self.assertFalse(
                app_install.safe_to_delete(path), f"must not delete {path!r}"
            )

    def test_cleanup_script_waits_for_the_process_and_self_deletes(self):
        script = app_install.cleanup_script(
            4321, [os.path.join(self.local, "FTC Whisper")], "s.ps1"
        )
        self.assertIn("Get-Process -Id 4321", script)
        self.assertIn("Remove-Item -LiteralPath $d -Recurse -Force", script)
        self.assertIn("s.ps1", script)

    def test_spawn_never_uses_detached_process(self):
        # DETACHED_PROCESS combined with CREATE_NO_WINDOW makes powershell.exe
        # exit 0 without running -File. That exact pair silently broke every
        # in-app update up to v1.6.3; the uninstaller must not repeat it.
        code = "\n".join(
            line for line in inspect.getsource(app_install._spawn_cleanup).splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("DETACHED_PROCESS", code)
        self.assertIn("CREATE_NO_WINDOW", inspect.getsource(app_install))


class AppWiringTests(unittest.TestCase):
    def test_app_registers_against_the_stable_exe_path(self):
        import app

        src = inspect.getsource(app._register_application)
        # _startup_target() is the canonical %LOCALAPPDATA% copy the updater
        # maintains. Registering sys.executable would pin every shortcut to
        # whatever folder the user first ran the download from.
        self.assertIn("_startup_target()", src)
        self.assertNotIn("sys.executable", src)
        self.assertIn('getattr(sys, "frozen", False)', src)

    def test_uninstall_is_handled_before_the_single_instance_check(self):
        import app

        src = inspect.getsource(app._main)
        self.assertLess(
            src.index("_uninstall_requested()"),
            src.index("_ensure_single_instance()"),
            "the resident instance would kill the uninstaller",
        )


if __name__ == "__main__":
    unittest.main()
