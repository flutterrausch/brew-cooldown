# brew-cooldown

Wait before upgrading Homebrew packages. `brew-cooldown` adds a **seven-day local
observation cooldown** to formula and cask upgrades, including third-party taps.
It previews by default and defers candidates it cannot verify.

**Early-stage software:** this reduces exposure to newly changed packages; it does
not certify that a package is safe. See [Limits](#limits) before relying on it.

## Why this exists

Updating promptly is usually good maintenance. Installing every release immediately
also puts your machine among the first to run a compromised release. A cooldown
leaves time for a problem to be noticed and the release to be withdrawn or replaced.

William Woodruff's [We should all be using dependency cooldowns](https://blog.yossarian.net/2025/11/21/We-should-all-be-using-dependency-cooldowns)
explains this approach using examples of supply-chain attacks. It also highlights
its limits: some compromises stay undetected longer than any practical waiting
period. Seven days is this project's default tradeoff, not a proven safety threshold.

Other tools support this policy directly: see [GitHub's Dependabot cooldown configuration](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#cooldown)
and [npm's minimum release age](https://docs.npmjs.com/cli/install/#min-release-age).
This project applies an observation-based policy around Homebrew upgrades, including
dependencies and cask extraction tools.

A weekly update schedule alone is insufficient: it can still pick up something
published five minutes ago. The waiting period must belong to the candidate,
not to the interval between your update commands.

## Quick start

Requires **Python 3.10+** and **Homebrew** on your PATH. Developed and checked on
macOS with Homebrew 6.0.22; other Homebrew versions and Linux are not yet validated.
There are no runtime Python dependencies, GitHub tokens, or extra taps to configure.

```sh
git clone https://github.com/flutterrausch/brew-cooldown.git
cd brew-cooldown

brew update
./brew-cooldown                 # Record candidates and preview; no package installs
```

Expect `DEFER` messages on the first run. After at least seven days, refresh the
metadata and try upgrading:

```sh
brew update && ./brew-cooldown upgrade
```

Only candidates that still match their observations and pass the dependency checks
can upgrade. You can preview as often as you like; repeated observations of an
unchanged candidate do not restart its timer.

**The clock starts when this machine first observes a candidate**, not at its
upstream publication date. A release that is already a month old still waits seven
days after your first observation. Changed candidates restart their timer, so tools
with frequent releases may remain deferred for longer.

To put the command on your PATH, optionally install from the checkout with an
existing `pipx` installation:

```sh
pipx install .
brew-cooldown --help
```

This repository is the distribution source; there is no published PyPI package
or Homebrew formula. Running `./brew-cooldown` directly needs no installation.

## Usage

The examples below run from the checkout. After installing with pipx, use
`brew-cooldown` instead of `./brew-cooldown`.

```sh
./brew-cooldown preview                       # Same as no arguments
./brew-cooldown upgrade --days 14             # Use a longer observation period
./brew-cooldown upgrade --only node@24        # Select one installed package
./brew-cooldown upgrade --exclude asc         # Leave one package alone
./brew-cooldown preview --verbose             # Show every dependency blocker
```

Formulae and casks are both included automatically. `--only` and `--exclude` can
be repeated and accept short names or fully qualified names such as
`vendor/tap/package`. Prefer qualified names when different taps have namesakes.
There is no built-in `asc` exclusion; that example is a user-selected policy.

| Output | Meaning |
| --- | --- |
| `DEFER` | The candidate or a dependency is still cooling down, unverifiable, or encountered an error. |
| `SKIP` | The selected package is explicitly excluded. |
| `READY` | Preview checks passed. Casks still require archive dependency verification before upgrading. |
| `UPGRADE` | Verification passed and the wrapper is invoking Homebrew. |

### Pins and exclusions

Pinned packages are not selected as upgrade targets, and the wrapper never unpins
anything. Homebrew's own pinning rules still apply to dependency resolution.
Use [`brew pin`](https://docs.brew.sh/Versions#brew-pin) for a persistent Homebrew
pin, or `--exclude` for this invocation. An exclusion also blocks parents whose
checked dependency graph includes that package.

### Use in an update script

Once the command is on your PATH, replace unrestricted `brew upgrade` calls with:

```sh
brew update && brew-cooldown upgrade --days 7
```

Do not follow that with a plain `brew upgrade` or `brew upgrade --cask`: that would
bypass the deferrals. The wrapper covers only upgrades made through it. It does
not change your shell configuration, create scheduled jobs, or update npm packages.

### Urgent fixes

The tool does not recognize security advisories or automatically exempt security
fixes. A cooldown can delay a needed patch. Review urgent fixes individually and,
if necessary, use Homebrew directly for the specific package. That deliberately
bypasses this wrapper's policy. `--days` must be at least one; there is no force flag.

## How the checks work

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

## Further reading

- [We should all be using dependency cooldowns — William Woodruff](https://blog.yossarian.net/2025/11/21/We-should-all-be-using-dependency-cooldowns): the motivation and tradeoffs.
- [Dependabot options reference — GitHub](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#cooldown): native cooldown configuration for repository dependencies.
- [Minimum release age — npm](https://docs.npmjs.com/cli/install/#min-release-age): a publication-age policy enforced by the package resolver.
- [Homebrew manual](https://docs.brew.sh/Manpage) and [version management](https://docs.brew.sh/Versions): the underlying upgrade, dependency, and pinning behaviour.
