import copy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from brew_cooldown import Brew, CooldownError, DAY, Planner, fingerprint, main, observe, state_file


def formula(name="example", **changes):
    data = {"name": name, "full_name": name, "tap": "homebrew/core",
            "versions": {"stable": "1.0"}, "urls": {"stable": {"checksum": "a" * 64}},
            "ruby_source_checksum": {"sha256": "b" * 64}, "revision": 0,
            "installed": [], "outdated": True}
    data.update(changes)
    return data


def cask(name="app", **changes):
    data = {"token": name, "full_token": name, "tap": "homebrew/cask",
            "version": "1.0", "sha256": "a" * 64,
            "ruby_source_checksum": {"sha256": "b" * 64}, "depends_on": {}}
    data.update(changes)
    return data


class FakeBrew:
    def __init__(self, formulae=(), casks=(), deps=None):
        self.formulae, self.casks, self.graph = list(formulae), list(casks), deps or {}

    def deps(self, name):
        return self.graph.get(name, [])

    def info(self, kind, names):
        pool = self.formulae if kind == "formula" else self.casks
        field = "name" if kind == "formula" else "token"
        items = [copy.deepcopy(p) for p in pool if any(n in (p[field], p["tap"] + "/" + p[field]) for n in names)]
        return {"formulae": items if kind == "formula" else [], "casks": items if kind == "cask" else []}


class ObservationTests(unittest.TestCase):
    def test_boundary_and_replaced_candidate(self):
        entries = {}
        self.assertEqual(observe(entries, "p", "a", 100, 7), 7 * DAY)
        self.assertEqual(observe(entries, "p", "a", 100 + 7 * DAY - 1, 7), 1)
        self.assertEqual(observe(entries, "p", "a", 100 + 7 * DAY, 7), 0)
        self.assertEqual(observe(entries, "p", "b", 100 + 8 * DAY, 7), 7 * DAY)
        self.assertEqual(observe(entries, "p", "a", 100 + 9 * DAY, 7), 7 * DAY)

    def test_clock_rollback_resets(self):
        entries = {}
        observe(entries, "p", "a", 100, 7)
        self.assertEqual(observe(entries, "p", "a", 50, 7), 7 * DAY)

    def test_artifact_recipe_and_revision_changes_reset_identity(self):
        original = formula()
        for replacement in ({"revision": 1}, {"ruby_source_checksum": {"sha256": "c" * 64}},
                            {"bottle": {"stable": {"files": {"mac": {"sha256": "d" * 64}}}}}):
            self.assertNotEqual(fingerprint("formula", original), fingerprint("formula", original | replacement))

    def test_install_state_does_not_change_candidate(self):
        self.assertEqual(fingerprint("formula", formula()), fingerprint("formula", formula(installed=[{"version": "1.0"}], outdated=False)))

    def test_tap_helpers_reset_third_party_only(self):
        data = formula(tap="vendor/tap", tap_git_head="abc")
        self.assertNotEqual(fingerprint("formula", data), fingerprint("formula", data | {"tap_git_head": "def"}))
        self.assertEqual(fingerprint("formula", formula(tap_git_head="abc")), fingerprint("formula", formula(tap_git_head="def")))

    def test_unverifiable_candidates_rejected(self):
        for kind, data in [("cask", cask(version="latest")), ("cask", cask(sha256="no_check")),
                           ("formula", formula(urls={})), ("formula", formula(installed=[{"version": "HEAD-abc"}])),
                           ("formula", formula(tap="vendor/tap")), ("cask", cask(ruby_source_checksum={}))]:
            with self.subTest(data=data), self.assertRaises(CooldownError):
                fingerprint(kind, data)

    def test_git_commit_supported_but_moving_tag_rejected(self):
        fingerprint("formula", formula(urls={"stable": {"revision": "a" * 40}}))
        with self.assertRaises(CooldownError):
            fingerprint("formula", formula(urls={"stable": {"revision": "main"}}))

    def test_failed_and_interrupted_runs_preserve_changed_observations(self):
        for failure in (CooldownError("unresolved selection"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                with state_file(path) as state:
                    observe(state["candidates"], "p", "a" * 64, 100, 7)
                with self.assertRaises(type(failure)), state_file(path) as state:
                    observe(state["candidates"], "p", "b" * 64, 100 + 8 * DAY, 7)
                    raise failure
                with state_file(path) as state:
                    self.assertEqual(observe(state["candidates"], "p", "a" * 64,
                                             100 + 9 * DAY, 7), 7 * DAY)

    def test_state_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with state_file(path) as state:
                observe(state["candidates"], "p", "a" * 64, 100, 7)
            with state_file(path) as state:
                self.assertEqual(state["candidates"]["p"]["first_seen"], 100)
            path.write_text('{"schema": 2, "candidates": {}}')
            with self.assertRaises(CooldownError), state_file(path):
                pass


class PlannerTests(unittest.TestCase):
    def test_source_resource_tool_is_observed_and_excludable(self):
        brew = FakeBrew([formula("zstd")])
        entries = {}
        Planner(brew, entries, 7, 100).add("formula", formula())
        for exclusions in ([], ["zstd"]):
            planner = Planner(brew, entries, 7, 100 + 8 * DAY, exclusions)
            root = planner.add("formula", formula())
            with patch.object(brew, "source_deps", return_value=["zstd"], create=True):
                expanded = planner.source_closure({root})
            self.assertIn(("formula", "homebrew/core/zstd"), expanded)
            self.assertTrue(any("zstd" in r for r in planner.blockers(expanded)))

    def test_unverifiable_source_prevents_upgrade(self):
        planner = Planner(FakeBrew(), {}, 7, 100)
        root = planner.add("formula", formula())
        planner.waits[root] = 0
        with patch.object(planner.brew, "source_deps", side_effect=CooldownError("unchecksummed resource"), create=True):
            with self.assertRaisesRegex(CooldownError, "unchecksummed"):
                planner.source_closure({root})

    def test_blocked_cask_does_not_hide_other_mature_archive_dependencies(self):
        young, mature = cask("a-young"), cask("z-mature")
        brew = FakeBrew([formula("extractor")], [young, mature])
        entries = {}
        Planner(brew, entries, 7, 100).add("cask", mature)
        planner = Planner(brew, entries, 7, 100 + 8 * DAY)
        roots = {planner.add("cask", item) for item in (young, mature)}
        with patch.object(brew, "cask_archive_deps", return_value=[("formula", "extractor")], create=True) as archive:
            closure = planner.archive_closure(roots)
            archive.assert_called_once_with("homebrew/cask/z-mature")
        self.assertIn(("formula", "homebrew/core/extractor"), closure)

    def test_identity_change_preserves_unchanged_observations(self):
        original, other = formula(), formula("other")
        brew = FakeBrew([other])  # original disappeared or was renamed
        planner = Planner(brew, {}, 7, 100)
        keys = {planner.add("formula", item) for item in (original, other)}
        with self.assertRaisesRegex(CooldownError, "changed"):
            planner.verify(keys)
        self.assertNotIn("formula:homebrew/core/example", planner.entries)
        self.assertIn("formula:homebrew/core/other", planner.entries)

    def test_all_changed_fingerprints_are_invalidated_in_one_batch(self):
        brew = FakeBrew([formula(revision=1), formula("other", revision=1)])
        planner = Planner(brew, {}, 7, 100)
        keys = {planner.add("formula", formula(n)) for n in ("example", "other")}
        with self.assertRaisesRegex(CooldownError, "changed"):
            planner.verify(keys)
        self.assertEqual(planner.entries, {})

    def test_pinned_outdated_pkgconf_blocks_implicit_repair(self):
        planner = Planner(FakeBrew(), {}, 7, 100)
        planner.add("formula", formula("pkgconf", pinned=True, installed=[{"version": "0.9"}]))
        root = planner.add("cask", cask())
        self.assertTrue(any("pinned" in reason for reason in planner.blockers(planner.closure(root))))

    def test_new_archive_cask_is_deferred_without_fetching_it(self):
        app = cask()
        brew = FakeBrew(casks=[app, cask("helper")])
        entries = {}
        Planner(brew, entries, 7, 100).add("cask", app)
        planner = Planner(brew, entries, 7, 100 + 8 * DAY)
        root = planner.add("cask", app)
        with patch.object(brew, "cask_archive_deps", return_value=[("cask", "helper")], create=True) as archive:
            closure = planner.archive_closure({root})
            archive.assert_called_once_with("homebrew/cask/app")
        self.assertIn("helper", planner.blockers(closure)[0])

    def test_archive_extractor_dependency_gets_its_own_cooldown(self):
        app = cask()
        brew = FakeBrew([formula("extractor")], [app])
        entries = {}
        Planner(brew, entries, 7, 100).add("cask", app)
        planner = Planner(brew, entries, 7, 100 + 8 * DAY)
        root = planner.add("cask", app)
        with patch.object(brew, "cask_archive_deps", return_value=[("formula", "extractor")], create=True):
            closure = planner.archive_closure(planner.closure(root))
        self.assertEqual(len(closure), 2)
        self.assertIn("extractor", planner.blockers(closure)[0])

    def test_archive_query_error_aborts(self):
        app = cask()
        brew = FakeBrew(casks=[app])
        entries = {}
        Planner(brew, entries, 7, 100).add("cask", app)
        planner = Planner(brew, entries, 7, 100 + 8 * DAY)
        root = planner.add("cask", app)
        with patch.object(brew, "cask_archive_deps", side_effect=CooldownError("API changed"), create=True):
            with self.assertRaisesRegex(CooldownError, "API changed"):
                planner.archive_closure({root})

    def test_young_transitive_dependency_blocks_old_parent(self):
        parent, child = formula(), formula("child")
        brew = FakeBrew([parent, child], deps={"homebrew/core/example": ["child"]})
        entries = {}
        initial = Planner(brew, entries, 7, 100)
        initial.add("formula", parent)
        later = Planner(brew, entries, 7, 100 + 8 * DAY)
        root = later.add("formula", parent)
        reasons = later.blockers(later.closure(root))
        self.assertEqual(len(reasons), 1)
        self.assertIn("child", reasons[0])

    def test_cask_formula_and_cask_dependencies(self):
        app = cask(depends_on={"formula": ["lib"], "cask": ["helper"]})
        brew = FakeBrew([formula("lib")], [app, cask("helper")])
        planner = Planner(brew, {}, 7, 100)
        closure = planner.closure(planner.add("cask", app))
        self.assertEqual(len(closure), 3)
        self.assertEqual(len(planner.blockers(closure)), 3)

    def test_pkgconf_repair_is_gated_even_for_cask(self):
        brew = FakeBrew()
        planner = Planner(brew, {}, 7, 100)
        planner.add("formula", formula("pkgconf", installed=[{"version": "1.0"}]))
        closure = planner.closure(planner.add("cask", cask()))
        self.assertIn(("formula", "homebrew/core/pkgconf"), closure)

    def test_alias_exclusion_blocks_root_and_parent(self):
        pkgconf = formula("pkgconf", aliases=["pkg-config"])
        brew = FakeBrew([pkgconf], deps={"homebrew/core/example": ["pkgconf"]})
        planner = Planner(brew, {}, 7, 100, ["pkg-config"])
        child = planner.add("formula", pkgconf)
        parent = planner.add("formula", formula())
        planner.resolve_exclusions()
        self.assertTrue(planner.excluded(child))
        self.assertTrue(any("pkgconf: excluded" in r for r in planner.blockers(planner.closure(parent))))

    def test_unresolved_and_ambiguous_exclusions_abort(self):
        planner = Planner(FakeBrew(), {}, 7, 100, ["typo"])
        with self.assertRaisesRegex(CooldownError, "unresolved"):
            planner.resolve_exclusions()
        planner = Planner(FakeBrew(), {}, 7, 100, ["tool"])
        planner.add("formula", formula("tool"))
        planner.add("formula", formula("tool", tap="vendor/tap", tap_git_head="abc"))
        with self.assertRaisesRegex(CooldownError, "ambiguous"):
            planner.resolve_exclusions()
        planner.exclusions = ["vendor/tap/tool"]
        planner.resolve_exclusions()
        self.assertFalse(planner.excluded(("formula", "homebrew/core/tool")))
        self.assertTrue(planner.excluded(("formula", "vendor/tap/tool")))

    def test_exclusion_resolves_uninstalled_dependency(self):
        planner = Planner(FakeBrew([formula("child")]), {}, 7, 100, ["child"])
        planner.resolve_exclusions()
        self.assertEqual(planner.exclusions, {"homebrew/core/child"})

    def test_excluded_dependency_blocks_parent(self):
        brew = FakeBrew([formula("child")], deps={"homebrew/core/example": ["child"]})
        planner = Planner(brew, {}, 7, 100, ["child"])
        reasons = planner.blockers(planner.closure(planner.add("formula", formula())))
        self.assertTrue(any("child: excluded" in r for r in reasons))

    def test_changed_candidate_aborts_before_upgrade(self):
        brew = FakeBrew([formula(revision=1)])
        planner = Planner(brew, {}, 7, 100)
        root = planner.add("formula", formula())
        with self.assertRaisesRegex(CooldownError, "changed"):
            planner.verify({root})
        self.assertNotIn(":".join(root), planner.entries)
        self.assertIn("changed", planner.blockers({root})[0])

    def test_unverifiable_candidate_erases_previous_age(self):
        planner = Planner(FakeBrew(), {}, 7, 100)
        key = planner.add("cask", cask())
        planner.add("cask", cask(sha256="no_check"))
        self.assertNotIn(":".join(key), planner.entries)


class SecurityWarningTests(unittest.TestCase):
    def scan(self, report, code=0, stderr=""):
        result = subprocess.CompletedProcess([], code, json.dumps(report), stderr)
        with patch("brew_cooldown.subprocess.run", return_value=result) as run, patch("builtins.print") as output:
            Brew().security_warnings()
        self.assertEqual(run.call_args.args[0],
                         ["brew", "vulns", "--severity=high", "--fix-available", "--json"])
        return [str(call.args[0]) for call in output.call_args_list]

    def test_findings_exit_one_is_not_a_scan_failure(self):
        finding = {"formula": "openssl@3", "version": "3.0.0", "vulnerabilities": [
            {"id": "CVE-2026-1234", "severity": "HIGH", "fixed_versions": ["3.0.1"]}],
            "patched": [{"id": "already-fixed"}]}
        lines = self.scan({"findings": [finding], "skipped_formulae": []}, code=1)
        warning = next(line for line in lines if "CVE-2026-1234" in line)
        self.assertTrue(warning.startswith("⚠️"))
        self.assertIn("https://osv.dev/vulnerability/CVE-2026-1234", warning)
        self.assertTrue(any("pins, and exclusions remain unchanged" in line for line in lines))
        self.assertFalse(any("already-fixed" in line or "scan incomplete" in line for line in lines))

    def test_skipped_packages_and_scanner_diagnostics_are_warnings(self):
        lines = self.scan({"findings": [], "skipped_formulae": ["vendor/tap/tool"]},
                          code=1, stderr="Installed source unknown; using current formula version\n")
        self.assertTrue(any(line.startswith("⚠️") and "Installed source unknown" in line for line in lines))
        self.assertTrue(any(line.startswith("⚠️") and "vendor/tap/tool" in line for line in lines))
        self.assertTrue(any(line.startswith("⚠️") and "incomplete" in line for line in lines))

    def test_invalid_output_is_nonfatal_and_not_a_clean_bill_of_health(self):
        lines = self.scan({"unexpected": []})
        self.assertTrue(any(line.startswith("⚠️") and "unavailable or incomplete" in line for line in lines))
        self.assertFalse(any(line.startswith("No high/critical") for line in lines))

    def test_timeout_is_nonfatal(self):
        with patch("brew_cooldown.subprocess.run", side_effect=subprocess.TimeoutExpired("brew", 60)), \
                patch("builtins.print") as output:
            Brew().security_warnings()
        self.assertTrue(any(str(call.args[0]).startswith("⚠️") for call in output.call_args_list))

    def test_advisory_text_cannot_inject_another_line(self):
        finding = {"formula": "tool\nFAKE", "version": "1", "vulnerabilities": [
            {"id": "CVE-1", "severity": "HIGH", "fixed_versions": ["2"]}]}
        lines = self.scan({"findings": [finding], "skipped_formulae": []}, code=1)
        self.assertFalse(any("\n" in line for line in lines))


class EnvironmentTests(unittest.TestCase):
    def test_force_refresh_is_removed_from_inherited_environment(self):
        with patch.dict("os.environ", {"HOMEBREW_FORCE_API_AUTO_UPDATE": "1"}):
            self.assertNotIn("HOMEBREW_FORCE_API_AUTO_UPDATE", Brew().env)

    def test_effective_brew_env_overrides_are_rejected(self):
        good = {k: "1" for k in ("HOMEBREW_NO_AUTO_UPDATE", "HOMEBREW_NO_INSTALL_CLEANUP",
                                  "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK")}
        for override in ({}, {"HOMEBREW_FORCE_API_AUTO_UPDATE": "1"},
                         {"HOMEBREW_NO_AUTO_UPDATE": ""}, {"HOMEBREW_NO_INSTALL_CLEANUP": ""},
                         {"HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": ""}):
            with self.subTest(override=override), patch.object(
                    Brew, "run", return_value="BREW_COOLDOWN_ENV=" + json.dumps(good | override)):
                if override:
                    with self.assertRaises(CooldownError):
                        Brew().check_environment()
                else:
                    Brew().check_environment()


class UpgradeTargetTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(Brew, "check_environment")
        guard.start()
        self.addCleanup(guard.stop)

    def test_unambiguous_target_uses_short_name(self):
        target = "vendor/tap/tool"
        with patch.object(Brew, "run", return_value="BREW_COOLDOWN_TARGET=" + json.dumps([target, target])):
            self.assertEqual(Brew().upgrade_name("formula", target), "tool")

    def test_namesake_and_installed_alias_redirects_are_rejected(self):
        requested = "vendor/tap/tool"
        for identities in (["homebrew/core/tool"] * 2, [requested, "vendor/tap/tool@2"]):
            with self.subTest(identities=identities), \
                    patch.object(Brew, "run", return_value="BREW_COOLDOWN_TARGET=" + json.dumps(identities)), \
                    patch("brew_cooldown.subprocess.run") as execute:
                with self.assertRaises(CooldownError):
                    Brew().upgrade("formula", requested)
                execute.assert_not_called()

    def test_missing_target_probe_result_blocks_upgrade(self):
        with patch.object(Brew, "run", return_value="unexpected output"):
            with self.assertRaises(CooldownError):
                Brew().upgrade_name("formula", "vendor/tap/tool")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(Brew, "check_environment")
        guard.start()
        self.addCleanup(guard.stop)
        source = patch.object(Brew, "source_deps", return_value=[])
        source.start()
        self.addCleanup(source.stop)
        scanner = patch.object(Brew, "security_warnings")
        self.scanner = scanner.start()
        self.addCleanup(scanner.stop)

    def test_cask_archive_failure_prevents_execution(self):
        candidate = cask(outdated=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with state_file(path) as state:
                observe(state["candidates"], "cask:homebrew/cask/app",
                        fingerprint("cask", candidate), 100, 7)
            with patch.object(Brew, "info", return_value={"formulae": [], "casks": [candidate]}), \
                    patch.object(Brew, "cask_archive_deps", side_effect=CooldownError("archive check failed")) as archive, \
                    patch.object(Brew, "upgrade") as upgrade, \
                    patch("brew_cooldown.time.time", return_value=100 + 8 * DAY), patch("builtins.print"):
                self.assertEqual(main(["preview", "--state", str(path)]), 0)
                archive.assert_not_called()
                self.assertEqual(main(["upgrade", "--state", str(path)]), 1)
                archive.assert_called_once()
                upgrade.assert_not_called()

    def run_tool(self, path, command="upgrade", aged=False, changed=False):
        candidate = formula()
        if aged:
            with state_file(path) as state:
                observe(state["candidates"], "formula:homebrew/core/example",
                        fingerprint("formula", candidate), 100, 7)
        calls = []

        def info(kind=None, names=()):
            value = candidate | {"revision": 1} if kind and changed else candidate
            return {"formulae": [value], "casks": []}

        with patch.object(Brew, "info", side_effect=info), patch.object(Brew, "deps", return_value=[]), \
                patch.object(Brew, "upgrade", side_effect=lambda *args: calls.append(args)), \
                patch("brew_cooldown.time.time", return_value=100 + 8 * DAY), patch("builtins.print"):
            rc = main([command, "--state", str(path)])
        return rc, calls

    def test_fresh_candidate_never_executes_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_tool(Path(directory) / "state.json"), (0, []))
        self.scanner.assert_called_once()

    def test_mature_candidate_executes_and_preview_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(self.run_tool(path, "preview", aged=True), (0, []))
            self.assertEqual(self.run_tool(path), (0, [("formula", "homebrew/core/example")]))

    def test_changed_candidate_never_executes_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(self.run_tool(path, aged=True, changed=True), (1, []))
            # Even a revert to the original version must start its clock again.
            self.assertEqual(self.run_tool(path), (0, []))

    def test_subprocess_disables_refresh_repairs_and_cleanup(self):
        with patch("brew_cooldown.subprocess.run") as run, \
                patch.object(Brew, "upgrade_name", return_value="app"):
            run.return_value.returncode = 0
            Brew().upgrade("cask", "vendor/tap/app")
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["brew", "upgrade", "--no-ask", "--cask", "app"])
            for setting in ("HOMEBREW_NO_AUTO_UPDATE", "HOMEBREW_NO_INSTALL_CLEANUP", "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK"):
                self.assertEqual(kwargs["env"][setting], "1")


if __name__ == "__main__":
    unittest.main()
