"""Config discovery, profile layering, validation, and precedence."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

import helpers  # noqa: F401  (adds the repository root to sys.path)

from src.config import ConfigError, MonitorSettings, find_config, resolve_settings
from src.events import LeakResource, LeakSeverity
from src.leak_detector import DEFAULT_THRESHOLDS

MB = 1024 * 1024


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def write(self, name: str, body: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def resolve(self, **kwargs) -> MonitorSettings:
        kwargs.setdefault("start", self.root)
        return resolve_settings(**kwargs)

    def assertConfigError(self, message: str, **kwargs) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.resolve(**kwargs)
        self.assertIn(message, str(caught.exception))


class DiscoveryTests(ConfigTestCase):
    def test_defaults_without_a_file(self) -> None:
        settings = self.resolve()
        self.assertEqual(settings, MonitorSettings())
        self.assertEqual(settings.describe(), "built-in defaults")

    def test_nearest_file_wins_and_parents_are_searched(self) -> None:
        outer = self.write(".drainrc", 'sample_interval = "1s"\n')
        nested = self.root / "project" / "src"
        nested.mkdir(parents=True)
        self.assertEqual(find_config(nested), outer)
        inner = self.write("project/draintracer.toml", 'sample_interval = "250ms"\n')
        self.assertEqual(find_config(nested), inner)
        self.assertEqual(self.resolve(start=nested).sample_interval_ms, 250)

    def test_dedicated_names_take_priority_in_a_directory(self) -> None:
        self.write("draintracer.toml", "")
        rc = self.write(".drainrc", "")
        self.assertEqual(find_config(self.root), rc)

    def test_shared_config_toml_needs_a_draintracer_table(self) -> None:
        self.write("config.toml", '[other_tool]\nname = "x"\n')
        self.assertIsNone(find_config(self.root))
        self.write("config.toml", "not = valid = toml")
        self.assertIsNone(find_config(self.root))
        shared = self.write("config.toml", '[draintracer]\ntimeout = "90s"\n[other_tool]\nx = 1\n')
        self.assertEqual(find_config(self.root), shared)
        self.assertEqual(self.resolve().timeout, 90.0)

    def test_discovery_can_be_disabled(self) -> None:
        self.write(".drainrc", 'timeout = "5m"\n')
        self.assertIsNone(self.resolve(discover=False).timeout)


class LayeringTests(ConfigTestCase):
    def test_origins_follow_individual_profile_and_command_line_overrides(self) -> None:
        self.write(".drainrc", '''
            sample_interval = "1s"
            budget_handles = 300
            [thresholds.memory]
            critical_delta = "100MB"
            [profiles.ci]
            sample_interval = "250ms"
            budget_handles = 200
            [profiles.ci.thresholds.memory]
            critical_slope = "1MB/s"
        ''')
        settings = self.resolve(profile="ci", overrides={"warmup": 2.0, "budgets": {"threads": 4}})
        self.assertEqual(settings.origins["sample_interval_ms"], "profile ci")
        self.assertEqual(settings.origins["warmup"], "command line")
        self.assertEqual(settings.origins["budgets.handles"], "profile ci")
        self.assertEqual(settings.origins["budgets.threads"], "command line")
        self.assertEqual(settings.origins["thresholds.memory.critical_delta"], "config file")
        self.assertEqual(settings.origins["thresholds.memory.critical_slope"], "profile ci")
        self.assertNotIn("timeout", settings.origins)
        with self.assertRaises(TypeError):
            settings.origins["warmup"] = "changed"

    def test_origin_metadata_does_not_change_settings_equality_or_noop_overrides(self) -> None:
        explicit = self.resolve(discover=False, overrides={"warmup": 0.0})
        self.assertEqual(explicit, MonitorSettings())
        empty = self.resolve(discover=False, overrides={"budgets": None, "thresholds": None})
        self.assertEqual(dict(empty.origins), {})

    CONFIG = """
        sample_interval = "250ms"
        windows = ["30s", "10s", "30s"]
        fail_on = "medium"
        report_dir = "out/reports"

        [thresholds.memory]
        critical_delta = "200MB"
        critical_slope = "20MB/s"

        [profiles.game]
        windows = ["30s", "60s", "2m"]
        timeout = "10m"
        [profiles.game.thresholds.memory]
        critical_slope = "3GB/min"

        [profiles.ci]
        fail_on = "low"
        timeout = 120
    """

    def test_base_settings_and_units(self) -> None:
        self.write(".drainrc", self.CONFIG)
        settings = self.resolve()
        self.assertEqual(settings.sample_interval_ms, 250)
        self.assertEqual(settings.windows, (10.0, 30.0))
        self.assertEqual(settings.fail_on, LeakSeverity.MEDIUM)
        self.assertEqual(Path(settings.report_dir), self.root / "out" / "reports")
        memory = settings.thresholds[LeakResource.MEMORY]
        self.assertEqual((memory.critical_delta, memory.critical_slope), (200 * MB, 20 * MB))
        self.assertEqual(
            settings.thresholds[LeakResource.HANDLES], DEFAULT_THRESHOLDS[LeakResource.HANDLES]
        )
        self.assertIsNone(settings.profile)
        self.assertTrue(settings.describe().startswith("settings from "))

    def test_profile_overrides_the_base_field_by_field(self) -> None:
        self.write(".drainrc", self.CONFIG)
        settings = self.resolve(profile="game")
        self.assertEqual(settings.windows, (30.0, 60.0, 120.0))
        self.assertEqual(settings.timeout, 600.0)
        self.assertEqual(settings.sample_interval_ms, 250)  # inherited from the base
        memory = settings.thresholds[LeakResource.MEMORY]
        self.assertEqual(memory.critical_delta, 200 * MB)  # inherited
        self.assertEqual(memory.critical_slope, 3 * 1024 * MB / 60)
        self.assertEqual(settings.profile, "game")
        self.assertIn("profile game from", settings.describe())

    def test_default_profile_and_flags_take_precedence_in_order(self) -> None:
        self.write(".drainrc", 'default_profile = "ci"\n' + textwrap.dedent(self.CONFIG))
        self.assertEqual(self.resolve().profile, "ci")
        self.assertEqual(self.resolve().fail_on, LeakSeverity.LOW)
        self.assertEqual(self.resolve(profile="game").profile, "game")
        flags = self.resolve(overrides={"fail_on": LeakSeverity.CRITICAL, "timeout": 5.0})
        self.assertEqual((flags.fail_on, flags.timeout), (LeakSeverity.CRITICAL, 5.0))

    def test_draintracer_table_works_in_any_file(self) -> None:
        path = self.write("custom.toml", '[draintracer]\n[draintracer.profiles.x]\ntimeout = "1m"\n')
        self.assertEqual(self.resolve(config_path=path, profile="x").timeout, 60.0)

    def test_report_dir_is_relative_to_the_settings_file(self) -> None:
        absolute = self.root / "absolute-reports"
        self.write(".drainrc", 'report_dir = "reports"\n[profiles.abs]\n'
                   f'report_dir = "{absolute.as_posix()}"\n')
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        self.assertEqual(Path(self.resolve(start=nested).report_dir), self.root / "reports")
        self.assertEqual(Path(self.resolve(profile="abs").report_dir), absolute)
        flag = self.resolve(overrides={"report_dir": "here"})
        self.assertEqual(flag.report_dir, "here")  # flags stay relative to the working directory

    def test_explicit_path_skips_discovery(self) -> None:
        self.write(".drainrc", 'timeout = "1m"\n')
        other = self.write("elsewhere/settings.toml", 'timeout = "2m"\n')
        self.assertEqual(self.resolve(config_path=other).timeout, 120.0)


class ValidationTests(ConfigTestCase):
    def test_typos_are_reported_with_a_suggestion(self) -> None:
        self.write(".drainrc", "[thresholds.memory]\ncritical_slop = 5\n")
        self.assertConfigError("unknown setting thresholds.memory.critical_slop "
                               "(did you mean thresholds.memory.critical_slope?)")

    def test_errors_name_the_file(self) -> None:
        path = self.write(".drainrc", 'timeout = "soon"\n')
        self.assertConfigError(f"{path}: timeout: expected a duration")

    def test_invalid_values(self) -> None:
        cases = {
            'fail_on = "never"': "fail_on must be one of low, medium, high, critical",
            'sample_interval = "10ms"': "sample_interval must be between 100ms and 1m",
            "windows = []": "windows must be a list",
            'windows = ["2h"]': "at most 1h",
            "timeout = 0": "timeout must be greater than zero",
            'report_dir = " "': "report_dir must be a non-empty path",
            "[thresholds.gpu]\ncritical_delta = 1": "unknown setting thresholds.gpu",
            '[thresholds.memory]\ncritical_delta = "5 parsecs"': "unknown size unit",
            '[thresholds.handles]\ncritical_slope = "20/fortnight"': "unknown rate period",
            "[thresholds.threads]\ncritical_delta = 0": "must be greater than zero",
            "profiles = 3": "profiles must be tables",
            'default_profile = "nope"': "default_profile 'nope' is not one of",
            "this is not toml": "not valid TOML",
        }
        for body, message in cases.items():
            with self.subTest(body=body):
                self.write(".drainrc", body + "\n")
                self.assertConfigError(message)

    def test_missing_profiles(self) -> None:
        self.write(".drainrc", "[profiles.ci]\n[profiles.game]\n")
        self.assertConfigError("profile 'gpu' is not defined (available: ci, game)", profile="gpu")
        (self.root / ".drainrc").unlink()
        self.assertConfigError("--profile ci needs a config file", profile="ci")

    def test_windows_must_hold_enough_samples(self) -> None:
        self.write(".drainrc", 'sample_interval = "1s"\nwindows = ["3s", "30s"]\n')
        self.assertConfigError("the 3s window is too short for a 1s sample interval")
        self.assertConfigError(
            "too short", overrides={"sample_interval_ms": 2000, "windows": (5.0,)}
        )

    def test_unreadable_explicit_path(self) -> None:
        self.assertConfigError("cannot read the file", config_path=self.root / "missing.toml")
        self.assertConfigError("this is a directory", config_path=self.root)

    def test_files_that_are_not_utf8(self) -> None:
        (self.root / ".drainrc").write_bytes(b'\xff\xfe timeout = "5s"\n')
        self.assertConfigError("the file is not UTF-8 text")
        (self.root / ".drainrc").unlink()
        # A shared config.toml that is not even text is simply not ours.
        (self.root / "config.toml").write_bytes(b"\xff\xfe\x00junk")
        self.assertIsNone(find_config(self.root))


class WarmupSettingsTests(ConfigTestCase):
    def test_warmup_from_file_profile_and_flags(self) -> None:
        self.write(".drainrc", 'warmup = "10s"\n[profiles.slow]\nwarmup = "1m"\n')
        self.assertEqual(self.resolve().warmup, 10.0)
        self.assertEqual(self.resolve(profile="slow").warmup, 60.0)
        self.assertEqual(self.resolve(overrides={"warmup": 0.0}).warmup, 0.0)
        self.assertEqual(MonitorSettings().warmup, 0.0)

    def test_warmup_limits(self) -> None:
        self.write(".drainrc", 'warmup = "25h"\n')
        self.assertConfigError("warmup must be at most 24h")
        self.write(".drainrc", 'warmup = "-1s"\n')
        self.assertConfigError("warmup: expected a duration")

    def test_a_time_limit_inside_the_warmup_is_rejected(self) -> None:
        self.assertConfigError(
            "the 30s time limit ends before the 1m warm-up does",
            discover=False, overrides={"warmup": 60.0, "timeout": 30.0},
        )

    def test_warnings_when_no_window_can_fill(self) -> None:
        settings = self.resolve(discover=False, overrides={"warmup": 10.0, "timeout": 15.0})
        (warning,) = settings.warnings()
        self.assertIn("leaves 5s of leak detection after the 10s warm-up", warning)
        self.assertIn("the 10s window needs 8.5s", warning)
        roomy = self.resolve(discover=False, overrides={"warmup": 10.0, "timeout": 30.0})
        self.assertEqual(roomy.warnings(), [])
        self.assertEqual(self.resolve(discover=False).warnings(), [])


class ExampleFileTests(unittest.TestCase):
    def test_every_example_profile_resolves(self) -> None:
        example = Path(__file__).resolve().parents[1] / "examples" / "drainrc.toml"
        for profile in (None, "ci", "heavy", "strict"):
            with self.subTest(profile=profile):
                settings = resolve_settings(config_path=example, profile=profile)
                self.assertEqual(settings.profile, profile)
        heavy = resolve_settings(config_path=example, profile="heavy")
        self.assertEqual(heavy.thresholds[LeakResource.MEMORY].critical_delta, 4 * 1024 * MB)


class CheckpointSettingsTests(ConfigTestCase):
    def test_checkpoint_paths_and_cadence_are_opt_in_and_layered(self) -> None:
        self.assertIsNone(self.resolve(discover=False).checkpoint)
        self.write(".drainrc", 'checkpoint = "state/session.json"\n[profiles.ci]\ncheckpoint_interval = "1s"\n')
        settings = self.resolve(profile="ci")
        self.assertEqual(Path(settings.checkpoint), self.root / "state/session.json")
        self.assertEqual(settings.checkpoint_interval, 1.0)

    def test_invalid_checkpoint_settings_fail_before_startup(self) -> None:
        for text, error in (('checkpoint = ""', "non-empty path"),
                            ('checkpoint_interval = "500ms"', "between 1s and 1h"),
                            ('checkpoint_interval = "2h"', "between 1s and 1h")):
            with self.subTest(text=text):
                self.write(".drainrc", text)
                self.assertConfigError(error)


class PolicySettingsTests(ConfigTestCase):
    def test_policies_come_from_files_and_default_off(self) -> None:
        self.assertFalse(self.resolve(discover=False).fail_on_inconclusive)
        self.assertFalse(self.resolve(discover=False).require_full_coverage)
        self.write(".drainrc", """
            fail_on_inconclusive = true
            [profiles.ci]
            fail_on_target_error = true
            require_full_coverage = true
        """)
        settings = self.resolve(profile="ci")
        self.assertTrue(settings.fail_on_inconclusive)
        self.assertTrue(settings.fail_on_target_error)
        self.assertTrue(settings.require_full_coverage)

    def test_policies_must_be_booleans(self) -> None:
        for key in ("fail_on_target_error", "require_full_coverage"):
            with self.subTest(key=key):
                self.write(".drainrc", f'{key} = "yes"\n')
                self.assertConfigError(f"{key} must be true or false")


class ExperimentSettingsTests(ConfigTestCase):
    def test_count_budgets_reject_fractions_in_settings_files(self) -> None:
        for key in ("budget_handles", "budget_threads"):
            for value in ("1.5", '"2.25"'):
                with self.subTest(key=key, value=value):
                    self.write(".drainrc", f"{key} = {value}\n")
                    with self.assertRaises(ConfigError) as caught:
                        self.resolve()
                    self.assertIn(key, str(caught.exception))
                    self.assertIn("whole number", str(caught.exception))

    def test_count_budgets_accept_whole_valued_settings(self) -> None:
        self.write(".drainrc", "budget_handles = 2000\nbudget_threads = 40.0\n")
        self.assertEqual(dict(self.resolve().budgets), {"handles": 2000, "threads": 40})

    def test_labels_and_budgets_come_from_files_and_profiles(self) -> None:
        self.write(".drainrc", """
            workload_id = "import-1k"
            operations = 1000
            budget_private = "500MB"
            [profiles.ci]
            build = " 1.4.2 "
            budget_handles = 2000
        """)
        settings = self.resolve(profile="ci", overrides={"budgets": {"threads": 40.0}})
        self.assertEqual(settings.experiment.workload_id, "import-1k")
        self.assertEqual(settings.build, "1.4.2")
        self.assertEqual(settings.operations, 1000)
        self.assertEqual(
            dict(settings.budgets),
            {"private_bytes": 500 * MB, "handles": 2000, "threads": 40.0},
        )
        self.assertTrue(self.resolve(discover=False).experiment.empty)
        self.assertEqual(dict(self.resolve(discover=False).budgets), {})

    def test_bad_labels_and_budgets_are_rejected(self) -> None:
        self.write(".drainrc", "operations = 0\n")
        self.assertConfigError("operations must be greater than zero")
        self.write(".drainrc", 'budget_private = "lots"\n')
        self.assertConfigError("budget_private")
        self.write(".drainrc", "workload_id = 3\n")
        self.assertConfigError("workload_id must be a non-empty string")

    def test_handle_and_thread_budgets_are_whole_counts(self) -> None:
        self.write(".drainrc", "budget_handles = 1.5\n")
        self.assertConfigError("whole number")
        self.write(".drainrc", 'budget_threads = "2.25"\n')
        self.assertConfigError("budget_threads: ")
        # Whole-valued floats are counts, and rate thresholds stay fractional.
        self.write(".drainrc", """
            budget_threads = 64.0
            [thresholds.handles]
            critical_slope = "1.5/s"
        """)
        settings = self.resolve()
        self.assertEqual(dict(settings.budgets), {"threads": 64})
        self.assertEqual(settings.thresholds[LeakResource.HANDLES].critical_slope, 1.5)


if __name__ == "__main__":
    unittest.main()
