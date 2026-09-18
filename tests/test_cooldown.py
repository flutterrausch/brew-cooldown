import copy
import json
from pathlib import Path
import tempfile
import unittest
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

    def test_unverifiable_candidate_erases_previous_age(self):
        planner = Planner(FakeBrew(), {}, 7, 100)
        key = planner.add("cask", cask())
        planner.add("cask", cask(sha256="no_check"))
        self.assertNotIn(":".join(key), planner.entries)


class ExecutionTests(unittest.TestCase):
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

    def test_mature_candidate_executes_and_preview_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertEqual(self.run_tool(path, "preview", aged=True), (0, []))
            self.assertEqual(self.run_tool(path), (0, [("formula", "homebrew/core/example")]))

    def test_changed_candidate_never_executes_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_tool(Path(directory) / "state.json", aged=True, changed=True), (1, []))

    def test_subprocess_disables_refresh_repairs_and_cleanup(self):
        with patch("brew_cooldown.subprocess.run") as run:
            run.return_value.returncode = 0
            Brew().upgrade("cask", "vendor/tap/app")
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["brew", "upgrade", "--no-ask", "--cask", "vendor/tap/app"])
            for setting in ("HOMEBREW_NO_AUTO_UPDATE", "HOMEBREW_NO_INSTALL_CLEANUP", "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK"):
                self.assertEqual(kwargs["env"][setting], "1")


if __name__ == "__main__":
    unittest.main()
