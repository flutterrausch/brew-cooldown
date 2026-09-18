"""Homebrew observation cooldown. No third-party Python dependencies."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

DAY = 86400
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
NAME = re.compile(r"[A-Za-z0-9@+_.-]+(?:/[A-Za-z0-9@+_.-]+){0,2}\Z")
# Local installation state and tap-wide commits are not candidate changes.
LOCAL_FIELDS = {
    "installed", "installed_time", "linked_keg", "outdated", "pinned",
    "pinned_version", "tap_git_head", "bundle_version", "bundle_short_version",
    "analytics", "generated_date",
}


class CooldownError(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(kind, data):
    tap = data.get("tap")
    name = data.get("name") if kind == "formula" else data.get("token")
    if not isinstance(tap, str) or not isinstance(name, str):
        raise CooldownError("missing tap or package name")
    full = f"{tap}/{name}"
    if not NAME.fullmatch(full):
        raise CooldownError(f"invalid package identity: {full}")
    return kind, full


def fingerprint(kind, data):
    identity(kind, data)
    recipe = (data.get("ruby_source_checksum") or {}).get("sha256", "")
    if not SHA256.fullmatch(recipe):
        raise CooldownError("missing recipe checksum")
    if data.get("disabled"):
        raise CooldownError("package is disabled")
    if kind == "formula":
        if any(str(x.get("version", "")).startswith("HEAD") for x in data.get("installed", [])):
            raise CooldownError("installed HEAD formula is not supported")
        stable = data.get("urls", {}).get("stable", {})
        if (not data.get("versions", {}).get("stable")
                or not (SHA256.fullmatch(stable.get("checksum") or "")
                        or GIT_REVISION.fullmatch(stable.get("revision") or ""))):
            raise CooldownError("stable source has no checksum or immutable Git revision")
        files = (data.get("bottle", {}).get("stable") or {}).get("files", {})
        if any(not SHA256.fullmatch(f.get("sha256") or "") for f in files.values()):
            raise CooldownError("bottle has no SHA-256 checksum")
    elif data.get("version") in (None, "latest") or not SHA256.fullmatch(data.get("sha256") or ""):
        raise CooldownError("cask has a moving version or no SHA-256 checksum")
    candidate = {k: v for k, v in data.items() if k not in LOCAL_FIELDS}
    # Third-party recipes may load shared Ruby helpers elsewhere in their tap.
    if data["tap"] not in ("homebrew/core", "homebrew/cask"):
        if not data.get("tap_git_head"):
            raise CooldownError("third-party tap has no Git revision")
        candidate["tap_git_head"] = data["tap_git_head"]
    return digest(candidate)


def observe(entries, key, candidate, now, days):
    """Only the currently observed fingerprint accumulates age; A→B→A resets."""
    record = entries.get(key)
    if (not record or record["fingerprint"] != candidate
            or now < record["last_seen"]):
        record = {"fingerprint": candidate, "first_seen": now, "last_seen": now}
        entries[key] = record
    record["last_seen"] = now
    return max(0, days * DAY - (now - record["first_seen"]))


@contextlib.contextmanager
def state_file(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CooldownError("another brew-cooldown process is running") from exc
        if path.exists():
            try:
                state = json.loads(path.read_text())
                if state["schema"] != 1 or not isinstance(state["candidates"], dict):
                    raise ValueError("unsupported schema")
                for value in state["candidates"].values():
                    if (not SHA256.fullmatch(value["fingerprint"])
                            or not all(isinstance(value[k], (float, int)) and math.isfinite(value[k])
                                       for k in ("first_seen", "last_seen"))
                            or not 0 <= value["first_seen"] <= value["last_seen"]):
                        raise ValueError("invalid observation")
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise CooldownError(f"invalid state file {path}: {exc}") from exc
        else:
            state = {"schema": 1, "candidates": {}}
        yield state
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temp = Path(output.name)
            try:
                json.dump(state, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)


class Brew:
    def __init__(self):
        self.env = dict(os.environ)
        self.env.update({
            "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_INSTALL_CLEANUP": "1",
            "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
            "HOMEBREW_NO_ENV_HINTS": "1",
        })

    def run(self, *args):
        result = subprocess.run(["brew", *args], env=self.env, text=True, capture_output=True)
        if result.returncode:
            raise CooldownError(f"brew {' '.join(args)} failed: {result.stderr.strip()}")
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return result.stdout

    def info(self, kind=None, names=()):
        args = ["info", "--json=v2"]
        args += [f"--{kind}", *names] if kind else ["--installed"]
        try:
            return json.loads(self.run(*args))
        except ValueError as exc:
            raise CooldownError("Homebrew returned invalid JSON") from exc

    def security_warnings(self):
        """Advisories inform the user; they never change upgrade eligibility."""
        def warn(message):
            # Keep registry-provided text on one line without terminal controls.
            text = " ".join(str(message).split())
            text = "".join(c for c in text if c.isprintable())
            print(f"⚠️ {text}", flush=True)

        print("Checking installed formulae for high/critical advisories with released fixes…", flush=True)
        try:
            result = subprocess.run(
                ["brew", "vulns", "--severity=high", "--fix-available", "--json"],
                env=self.env, text=True, capture_output=True, timeout=60,
            )
            for line in result.stderr.splitlines():
                if line.strip():
                    warn(f"Security scanner: {line}")
            report = json.loads(result.stdout)
            findings, skipped = report["findings"], report["skipped_formulae"]
            if not isinstance(findings, list) or not isinstance(skipped, list):
                raise ValueError("unexpected report format")
            count = 0
            for finding in findings:
                for vuln in finding["vulnerabilities"]:
                    fixes = vuln["fixed_versions"]
                    if not isinstance(fixes, list):
                        raise ValueError("unexpected fixed versions")
                    warn(f"{finding['formula']} (scanned version {finding['version']}): "
                         f"{vuln['id']} [{vuln['severity']}]. "
                         f"Reported fixed versions: {', '.join(fixes)}. "
                         f"https://osv.dev/vulnerability/{quote(vuln['id'], safe='')}")
                    count += 1
            if count:
                warn("Review these potential security fixes. The Homebrew candidate is not verified "
                     "to fix them; cooldowns, pins, and exclusions remain unchanged.")
            if skipped:
                warn(f"Security scanner skipped {len(skipped)} formulae: {', '.join(skipped)}")
            # brew vulns normally exits 1 when it finds open vulnerabilities.
            if result.returncode not in (0, 1) or (result.returncode == 1 and not count):
                warn(f"Security scan incomplete (exit {result.returncode}); cooldown checks continue.")
            elif not count:
                print("No high/critical advisories with released fixes reported; coverage may be incomplete.")
            print("Security scan covers formulae, not casks.", flush=True)
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
            warn(f"Security scan unavailable or incomplete ({exc}); cooldown checks continue. "
                 "This does not mean installed packages are free of vulnerabilities.")

    def deps(self, name):
        result = self.run("deps", "--formula", "--full-name", "--union",
                          "--include-build", "--include-optional", "--include-implicit", name)
        names = result.splitlines()
        if any(not NAME.fullmatch(n) or n.startswith("-") for n in names):
            raise CooldownError("unexpected dependency output")
        return names

    def cask_archive_deps(self, name):
        # Archive formats can add dependencies absent from `brew info`/`brew deps`.
        # Only fetch and inspect; never call Installer.install or dependency installers.
        script = '''
require "json"
require "cask/cask_loader"
require "cask/download"
require "cask/installer"
cask = Cask::CaskLoader.load(JSON.parse(%s))
Cask::Download.new(cask, require_sha: true).fetch
dependencies = Cask::Installer.new(cask).cask_and_formula_dependencies.map do |dep|
  if dep.is_a?(Cask::Cask)
    ["cask", dep.full_name]
  else
    ["formula", dep.full_name]
  end
end
puts "BREW_COOLDOWN_DEPS=" + JSON.generate(dependencies)
''' % json.dumps(json.dumps(name))
        output = self.run("ruby", "-e", script)
        lines = [line for line in output.splitlines() if line.startswith("BREW_COOLDOWN_DEPS=")]
        try:
            if len(lines) != 1:
                raise ValueError("missing dependency result")
            dependencies = json.loads(lines[0].split("=", 1)[1])
            if not isinstance(dependencies, list):
                raise ValueError("invalid dependencies")
            for kind, dep in dependencies:
                if kind not in ("formula", "cask") or not NAME.fullmatch(dep) or dep.startswith("-"):
                    raise ValueError("invalid dependency")
            return dependencies
        except (ValueError, TypeError) as exc:
            raise CooldownError(f"could not inspect cask archive dependencies: {name}") from exc

    def upgrade(self, kind, name):
        # No arbitrary arguments: users cannot accidentally enable HEAD/greedy/source overrides.
        result = subprocess.run(["brew", "upgrade", "--no-ask", f"--{kind}", name], env=self.env)
        if result.returncode:
            raise CooldownError(f"upgrade failed for {name} (exit {result.returncode})")


class Planner:
    def __init__(self, brew, entries, days, now, exclusions=()):
        self.brew, self.entries, self.days, self.now = brew, entries, days, now
        self.exclusions = exclusions
        self.data = {}
        self.aliases = {}
        self.waits = {}
        self.errors = {}

    def add(self, kind, data):
        key = identity(kind, data)
        self.data[key] = data
        self.errors.pop(key, None)
        self.waits.pop(key, None)
        for alias in (key[1], data.get("full_name"), data.get("full_token")):
            if alias:
                self.aliases[kind, alias] = key
        state_key = ":".join(key)
        try:
            self.waits[key] = observe(self.entries, state_key, fingerprint(kind, data), self.now, self.days)
        except CooldownError as exc:
            self.errors[key] = str(exc)
            self.entries.pop(state_key, None)
        return key

    def load(self, kind, name):
        if (kind, name) in self.aliases:
            return self.aliases[kind, name]
        response = self.brew.info(kind, [name])
        items = response["formulae" if kind == "formula" else "casks"]
        if len(items) != 1:
            raise CooldownError(f"cannot resolve {kind} {name}")
        key = self.add(kind, items[0])
        self.aliases[kind, name] = key
        return key

    def closure(self, key):
        found = set()

        def visit(current):
            if current in found:
                return
            found.add(current)
            kind, name = current
            if kind == "formula":
                # brew deps resolves the transitive graph, including build/implicit deps.
                for dep in self.brew.deps(name):
                    found.add(self.load("formula", dep))
            else:
                deps = self.data[current].get("depends_on") or {}
                if set(deps) - {"formula", "cask", "macos", "arch"}:
                    raise CooldownError(f"unsupported cask dependencies for {name}")
                for dep_kind in ("formula", "cask"):
                    values = deps.get(dep_kind, [])
                    if isinstance(values, str):
                        values = [values]
                    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                        raise CooldownError(f"unsupported dependencies for {name}")
                    for dep in values:
                        visit(self.load(dep_kind, dep))

        visit(key)
        # Homebrew can repair pkgconf after ANY upgrade following a macOS SDK change.
        pkgconf = ("formula", "homebrew/core/pkgconf")
        if pkgconf in self.data and self.data[pkgconf].get("installed"):
            visit(pkgconf)
        return found

    def excluded(self, key):
        return any(x == key[1] or x == key[1].rsplit("/", 1)[-1] for x in self.exclusions)

    def archive_closure(self, closure):
        checked = set()
        expanded = set(closure)
        while True:
            pending = {k for k in expanded if k[0] == "cask"} - checked
            if not pending:
                return expanded
            for key in sorted(pending):
                # Newly discovered casks must mature before even fetching their archive.
                if self.blockers({key}):
                    return expanded
                for kind, dep in self.brew.cask_archive_deps(key[1]):
                    expanded.update(self.closure(self.load(kind, dep)))
                checked.add(key)

    def blockers(self, closure):
        reasons = []
        for key in sorted(closure):
            name = key[1]
            if self.excluded(key):
                reasons.append(f"{name}: excluded")
            elif key in self.errors:
                reasons.append(f"{name}: {self.errors[key]}")
            elif self.waits[key] > 0:
                reasons.append(f"{name}: {self.waits[key] / DAY:.1f}d remaining")
        return reasons

    def verify(self, closure):
        for kind in ("formula", "cask"):
            keys = {k for k in closure if k[0] == kind}
            if not keys:
                continue
            result = self.brew.info(kind, sorted(k[1] for k in keys))
            items = result["formulae" if kind == "formula" else "casks"]
            fresh = {identity(kind, item): item for item in items}
            if keys != fresh.keys():
                for key in keys:
                    self.entries.pop(":".join(key), None)
                    self.errors[key] = "package identity changed during verification"
                raise CooldownError("package identity changed during verification; run again")
            for key in keys:
                try:
                    if fingerprint(kind, fresh[key]) != fingerprint(kind, self.data[key]):
                        raise CooldownError(f"candidate changed during verification: {key[1]}; run again")
                except CooldownError as exc:
                    self.entries.pop(":".join(key), None)
                    self.errors[key] = str(exc)
                    raise


def positive_days(value):
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("days must be at least 1")
    return days


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "upgrade"), nargs="?", default="preview")
    parser.add_argument("--days", type=positive_days, default=7)
    parser.add_argument("--only", action="append", default=[], metavar="NAME", help="select installed package (repeatable)")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME", help="exclude package, including as dependency")
    parser.add_argument("--verbose", action="store_true", help="show every dependency blocker")
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "brew-cooldown/state.json")
    args = parser.parse_args(argv)
    try:
        brew = Brew()
        installed = brew.info()
        with state_file(args.state) as state:
            planner = Planner(brew, state["candidates"], args.days, time.time(), args.exclude)
            roots = []
            matched = set()
            for kind, field in (("formula", "formulae"), ("cask", "casks")):
                for item in installed[field]:
                    try:
                        key = planner.add(kind, item)
                    except CooldownError as exc:
                        print(f"DEFER {item.get('full_name', item.get('token', '?'))}: {exc}")
                        continue
                    selected = {n for n in args.only if n in (key[1], key[1].rsplit("/", 1)[-1])}
                    matched.update(selected)
                    if args.only and not selected:
                        continue
                    if key in planner.errors and not item.get("outdated"):
                        print(f"DEFER {key[1]}: {planner.errors[key]}")
                    if item.get("outdated") and not item.get("pinned"):
                        roots.append(key)
            if set(args.only) - matched:
                raise CooldownError(f"not installed or unresolved: {', '.join(sorted(set(args.only) - matched))}")
            brew.security_warnings()
            print(f"{args.command.capitalize()}: {args.days}-day observation cooldown; {len(roots)} outdated candidates")
            failed = False
            for key in roots:
                if planner.excluded(key):
                    print(f"SKIP {key[1]}: excluded")
                    continue
                try:
                    closure = planner.closure(key)
                    blockers = planner.blockers(closure)
                    if blockers:
                        blockers.sort(key=lambda r: ("remaining" in r, not r.startswith(key[1] + ":")))
                        shown = blockers if args.verbose else blockers[:3]
                        extra = f"; +{len(blockers) - len(shown)} more (--verbose)" if len(shown) < len(blockers) else ""
                        print(f"DEFER {key[1]}: " + "; ".join(shown) + extra)
                        continue
                    if args.command == "upgrade":
                        planner.verify(closure)
                        if planner.closure(key) != closure:
                            raise CooldownError("dependency graph changed; run again")
                        if any(k[0] == "cask" for k in closure):
                            closure = planner.archive_closure(closure)
                            blockers = planner.blockers(closure)
                            if blockers:
                                print(f"DEFER {key[1]}: " + "; ".join(blockers))
                                continue
                            planner.verify(closure)
                        print(f"UPGRADE {key[1]}", flush=True)
                        brew.upgrade(*key)
                    else:
                        pending = "; archive dependency check pending" if any(k[0] == "cask" for k in closure) else ""
                        print(f"READY {key[1]} ({len(closure) - 1} dependency candidates checked{pending})")
                except CooldownError as exc:
                    print(f"DEFER {key[1]}: {exc}", file=sys.stderr)
                    failed = True
            print(f"Observations saved to {args.state}")
        return int(failed)
    except (CooldownError, OSError) as exc:
        print(f"brew-cooldown: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
