# Releasing Superboard

**This is guidance, not law — a living document.** It collects what we currently
think a good release looks like: the technical steps and the product checks around
them. Read it before a release, pick what applies with judgment, skip what does not
(say so in the release notes' working thread), and — the one real rule — **leave it
better than you found it**: every release that teaches something edits this file in
the same commit. The owner takes the calls marked **OWNER**.

Why guidance: a public release is a *promise*, not a version bump. The internal board
counts every change by size (`Build 6.x`, see `superboard/CHANGELOG.md`); the public
number (`pyproject.toml`, `RELEASES.md`) moves only when we consciously say "this is
what a stranger gets now".

## 0 · Decide that a release is due

- **OWNER** picks the number and the step: patch = fixes only · minor = something new
  a user notices · major = something to read before upgrading.
- Write down, in one sentence, what this release is *for* — it becomes the first line
  of the release notes.

## 1 · Bring the public projection up to date

- From the origin board, run its port status (`port_to_superboard.py`): classify every
  reported file as launch-relevant, intentionally divergent, superseded, or deferred.
  Port launch-relevant changes and record the rest; a raw count is not a release gate.
- Compare both build numbers before `sync-version`. Run it only when the origin is
  actually ahead; never use it to downgrade a candidate with independent fixes.
- Run `scripts/leak_scan.py --history` on purpose; the hooks are a backstop.

## 2 · Test what a stranger actually gets

- Fresh install from a clean environment using the *built artifact*, not the source
  tree: `scripts/testrig.sh fresh`.
- Installed smoke: `scripts/smoke-installed.sh`, plus green macOS smoke on the release
  commit.
- **E2E by hand:** empty workspace → first run → first item → agent reply →
  thread → archive. `scripts/record-installed-e2e.py` records the proof.
- Every supported OS/Python claimed in the README gets at least the smoke; remove
  untested claims.

## 3 · Product surface — does the outside still match the inside?

- Read the README top to bottom as a stranger and run every command verbatim.
- Compare screenshots and demos with the E2E proof. Changed UI means re-recording.
- Recheck `PITCH.md`, `SUPPORT.md`, package metadata, and the sandbox.

## 4 · Write the release

- From the origin board: `port_to_superboard.py release <x.y.z>` sets the public
  version and prepares the `RELEASES.md` entry.
- Curate the notes into 3–8 user-facing lines; delete the raw internal headings.
- **OWNER** reads the notes and README diff. This is the go/no-go.

## 5 · Publish

- Before the first release, create the GitHub `pypi` environment with required manual
  approval and register a PyPI pending publisher for the GitHub owner reported by
  `gh repo view --json nameWithOwner`, repository `superboard`, workflow
  `publish-to-pypi.yml`, environment `pypi`.
- Commit (`chore(release): v<x.y.z>`), tag `v<x.y.z>`, and push only after the explicit
  go. Never push directly to main.
- Use a public no-reply identity for commit and annotated-tag metadata. The history
  leak gate scans commit objects as well as blobs; inspect its author/committer output.
- The tag workflow builds, checks, installs, smokes, and publishes the same artifact via
  Trusted Publishing. Never use a local PyPI token.
- Verify from outside: clean environment, `uvx superboard --version`, first run,
  PyPI page, GitHub release page, README images and links.

## 6 · After

- Announce through the launch channels selected for this release.
- Watch the first days for issues, install failures, and CI failures.
- Put anything learned about the ritual back into this file.

## Hard lines

No release with an unresolved launch-relevant port item, a red installed smoke, or a
README claim nobody verified this round.

## Changelog of this document

- 2026-08-25 · first version; deliberately expected to evolve after the first release.
- 2026-08-26 · added the concrete pending-publisher identity, approval environment,
  and same-artifact build/smoke/publish contract.
- 2026-08-26 · added commit-metadata privacy and curated GitHub release notes after
  the final launch challenge found that clean blobs were not the whole public surface.
- 2026-08-27 · 0.2.0: a release PR opened right after its branch push did not get the
  required `macOS / Python` pull_request checks; a follow-up commit on the branch
  (this line) triggers them. Version bump and RELEASES.md were edited by hand when
  the port script could not run — same result, check `pyproject.toml` twice.
- 2026-09-05 · 0.3.0: the public `Build` number has its own series since 0.1.0 (the
  public CHANGELOG carries 6.21.x/6.22.0 entries that never existed in the origin), so
  `sync-version` and `port_to_superboard.py release` cannot be used — the release gate
  in `release` demands origin build == public build. Bump the public build in its own
  series (here 6.23.0), name the origin stand in the entry, and edit `pyproject.toml`
  and `RELEASES.md` by hand. Also: a release may deliberately port an OLDER origin
  commit than HEAD (here 04.09. instead of 05.09.) to leave out an untested feature —
  `git archive <commit>` + `git merge-file` against the ledger's base commit does this
  cleanly; the ledger then records the ported commit's hashes, not HEAD's.
