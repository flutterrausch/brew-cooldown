# Behaviour and limitations

[Back to the README](../README.md)

## How the checks work

Before planning upgrades, both commands run
`brew vulns --severity=high --fix-available --json` for installed formulae, including
pinned/excluded formulae and regardless of `--only`. Findings print warnings led
by `⚠️`, with the scanned version, advisory ID, severity, reported fixed versions,
and an OSV link. Already patched findings are not repeated.

These are advisory warnings, not verified security-upgrade classifications. A
released upstream fix may not be present in the Homebrew candidate. Homebrew can
also fall back to scanning current formula metadata when the installed source is
unknown; scanner diagnostics are retained as warnings. Skipped packages, unavailable
scanning, and timeouts are reported without changing upgrade eligibility or exit
status. The scan has a 60-second timeout and does not cover casks. No findings is
not evidence that every installed package is safe. Normal cooldown, pin, and
exclusion rules still apply.

The first observation starts the clock, even for a release published long ago.
Run `brew update` before invoking this tool to fetch current metadata. The tool
itself disables automatic Homebrew updates while inspecting and upgrading.
Both preview and upgrade record observations. Once a candidate reaches the age
threshold, it can be upgraded; no older version is selected instead.

Candidates are keyed by package kind and fully qualified tap/name. Their metadata
fingerprint includes source and bottle checksums, recipe checksum, version,
revision, dependencies, and cask installer artifacts. Formula sources pinned to a
full immutable Git commit are also supported. Local installed state and
unrelated official tap commits are excluded. For third-party taps, the entire tap
Git revision is included because recipes may load shared helpers: even unrelated
commits in that tap conservatively restart the timer.

A changed candidate restarts its clock, including a return to a previously observed
version. Unknown tap identity, missing checksums, HEAD installs, and checksum-free
or `latest` casks are deferred. Pinned packages are not selected for upgrade.

Formula dependency checks include build, optional, implicit, and transitive
dependencies reported by Homebrew. Cask formula and cask dependencies are checked
recursively. Even already installed dependency candidates must pass; this can defer
more upgrades than necessary. Exclusions also block parents that depend on them.
An installed `pkgconf` and its dependencies are checked because Homebrew can repair
it after an upgrade following a macOS SDK change.

Before an eligible cask is actually upgraded, its archive is downloaded and
checksum-verified. A small `brew ruby` query asks Homebrew's cask installer for
archive-dependent extraction tools and other dependencies, without installing
anything. Those candidates and their dependencies must also pass the cooldown.
Newly discovered dependencies may therefore cause another seven-day wait. Preview
does not download archives and labels eligible casks as pending this final check.

Eligible candidates are re-read immediately before each upgrade, and the dependency
graph is checked again. Automatic dependent upgrades/repairs and installation
cleanup are disabled. Upgrades run one selected package at a time with Homebrew's
checksum verification and existing tap trust controls intact.

## State and exit codes

State is stored in `$XDG_STATE_HOME/brew-cooldown/state.json`, defaulting to
`~/.local/state/brew-cooldown/state.json`. `--state PATH` overrides it. State writes
are atomic, overlapping wrapper runs are locked out, and invalid state aborts.
Deleting state restarts the observation period. A detected backward clock jump
also restarts it. Exit status is zero for normal policy deferrals, one for metadata,
verification, or upgrade errors, and two for invalid CLI arguments.

## Limits

This is a conservative cooldown, not a security scanner, sandbox, or transactional
package manager. A malicious release can remain undetected beyond seven days.
It observes metadata at invocation time; it cannot detect changes that occur and
are reverted between runs. The local clock and state must be trustworthy.

Do not run another Homebrew update/install/upgrade or edit taps concurrently.
Homebrew has no public atomic API for executing an immutable upgrade plan, so
there remains a gap between final verification and installation. The wrapper's
lock covers other wrapper invocations only. Future changes to Homebrew's implicit
installation behaviour may require updates to these checks. The archive query
uses Homebrew's internal Ruby API (verified against Homebrew 6.0.22); API errors
defer the upgrade rather than bypassing the check. This is the one integration
point that needs particular attention when supporting future Homebrew releases.

Homebrew itself, arbitrary Ruby in third-party recipes, installer/post-install
scripts, software those scripts download independently, and apps' own automatic
updaters are outside the gate. Loading a trusted tap's metadata may execute Ruby.
The wrapper never grants trust to a new tap. A cask's checksum covers its downloaded
archive/installer, not everything an installer might subsequently fetch.

Homebrew may omit auto-updating casks from its outdated set. This tool follows
Homebrew's installed metadata and does not force greedy updates or control an
app's own updater. Unverifiable casks are reported rather than given an exemption.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests use fake Homebrew metadata and temporary state; they do not install or
upgrade packages. Actual upgrade execution has not yet been validated end to end
on a live system.

Issues and contributions should include the Homebrew version, Python version,
operating system, command, and relevant output. Do not include credentials or
private tap contents.
