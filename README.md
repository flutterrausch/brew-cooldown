# brew-cooldown

Give new Homebrew updates a little time to settle.

`brew-cooldown` waits **seven days after first observing an unchanged candidate**
before upgrading it. It supports formulae, casks, and third-party taps, checks
dependencies, and previews by default.

## Why a cooldown?

A compromised release can reach your machine before anyone spots the problem.
Waiting gives maintainers and security researchers time to detect and withdraw it.
A weekly update schedule alone does not help: it can still install a release that
is five minutes old.

William Woodruff's [We should all be using dependency cooldowns](https://blog.yossarian.net/2025/11/21/We-should-all-be-using-dependency-cooldowns)
is a good introduction. Seven days is a practical default, not a guarantee of safety;
a cooldown can also delay an important security fix.

### Doesn't the package manager already do this?

**npm does:** its native [`min-release-age`](https://docs.npmjs.com/cli/install/#min-release-age)
setting filters newly resolved registry versions by publication age, including
transitive dependencies. For example, `npm update --min-release-age=7` sets a
seven-day minimum. This wrapper does not manage npm packages.

**Homebrew has no general user-facing upgrade cooldown yet** (checked September
2026). The [original proposal](https://github.com/Homebrew/brew/issues/21129) was
closed as not planned; a later [`brew outdated --cooldown-days` proposal](https://github.com/Homebrew/brew/issues/22000)
was closed as a duplicate. Homebrew instead added narrower cooldowns for
[npm/pip build helpers](https://github.com/Homebrew/brew/pull/21919) and
[npm/PyPI version bumps](https://github.com/Homebrew/brew/pull/21888).

This independent wrapper fills that gap using local observations rather than
trying to infer upstream release dates.

## Get started

You need **Python 3.10+** and **Homebrew** on your PATH. No Python dependencies,
GitHub token, or extra tap is needed.

```sh
git clone https://github.com/flutterrausch/brew-cooldown.git
cd brew-cooldown
brew update && ./brew-cooldown
```

The first run records candidates and shows `DEFER` messages. After seven days:

```sh
brew update && ./brew-cooldown upgrade
```

Unchanged, eligible candidates can now upgrade. Changed candidates start a fresh
waiting period. You can preview whenever you like without resetting unchanged
candidates. Even an old release waits seven days after your first observation.

## Everyday use

```sh
./brew-cooldown                         # Preview; no packages installed
./brew-cooldown upgrade                 # Apply eligible upgrades
./brew-cooldown upgrade --days 14       # Wait longer
./brew-cooldown upgrade --only node@24  # Select an installed package
./brew-cooldown upgrade --exclude asc   # Leave a package alone
./brew-cooldown --verbose               # Show all dependency blockers
```

Both formulae and casks are included. Repeat `--only` or `--exclude` as needed;
use `vendor/tap/package` to distinguish namesakes. Pinned packages are not selected
for upgrade, and nothing is unpinned. Explicit exclusions also block dependent
upgrades. Formula aliases such as `pkg-config` are accepted; unknown or ambiguous
exclusions abort before any upgrade. No packages are excluded by default.

Upgrades are deferred if Homebrew's short name or installed alias would select a
different package. This avoids both source mix-ups and implicit trust grants from
fully qualified upgrade arguments.

Optionally run `pipx install .` to put `brew-cooldown` on your PATH. In an update
script, use `brew update && brew-cooldown upgrade` in place of unrestricted
`brew upgrade` calls. Following it with plain `brew upgrade` would bypass the gate.

## A few things to know

- Preview and upgrade check installed formulae with `brew vulns`. **⚠️ warnings**
  flag high/critical advisories with released fixes and link to details. They never
  bypass the cooldown, pins, or exclusions; casks are not covered by this scan.
- **Early-stage:** checked on macOS with Homebrew 6.0.22. Actual upgrades have not
  yet been tested end to end on a live system.
- Dependencies can extend the wait. Casks may need an archive download during
  `upgrade` to discover extraction dependencies. Complete formula recipes are also
  checked before upgrading; preview does not download archives.
- Unknown or checksum-free candidates are deferred. Frequent releases can keep a
  package waiting. Review urgent security fixes individually.
- Avoid concurrent Homebrew commands or tap edits. Homebrew cannot atomically
  execute an immutable checked plan, so a verification-to-install gap remains.
- The gate does not cover Homebrew itself, arbitrary recipe/installer code, or an
  app's own updater. Reading third-party tap metadata can itself execute Ruby.

Observations live in `~/.local/state/brew-cooldown/state.json` (or under
`$XDG_STATE_HOME`). `--state PATH` overrides this; deleting state restarts the clock.

See [behaviour and limitations](docs/behaviour.md) for dependency handling, state,
exit codes, and Homebrew API compatibility. Run the tests with:

```sh
python3 -m unittest discover -s tests -v
```
