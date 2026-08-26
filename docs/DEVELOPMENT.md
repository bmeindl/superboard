# Development and test rigs

Contributor detail lives here so the public README can stay a one-minute product
orientation.

## Contributor map

- `superboard/server.py` owns HTTP routes, workspace mutations, runner hand-off,
  and the API contracts exercised by the server tests.
- `superboard/index.html` is the deliberately self-contained board UI; search for
  the relevant endpoint or DOM id before editing its larger sections.
- `superboard/onboarding.py` owns the files and starter cards created for a fresh
  workspace. `superboard/paths.py` centralizes workspace file compatibility.
- `scripts/` contains the installed-wheel, leak, sandbox, and release test rigs;
  `.github/workflows/` invokes the same contracts on hosted runners.

## Fictional sandbox

```sh
scripts/sandbox.sh
```

This serves the fictional demo workspace on `http://localhost:47899`. Nothing
outside `sandbox/` is read or written.

## Fresh-wheel test rig

```sh
scripts/testrig.sh fresh    # wipe, build, install, and serve a true first install
scripts/testrig.sh up       # rebuild and serve on http://localhost:47850
scripts/testrig.sh stop
scripts/testrig.sh status
scripts/testrig.sh reset
```

Use `fresh` for first-run claims. `up` on a workspace already clicked through is
not a first install. The rig has its own fixed port and gitignored `.testrig/`
workspace, and installs a built wheel with `uvx --no-cache`. It strips the
operator's Claude settings, skills, and MCP servers so the agent behaves like a
fresh install.

This isolates files, ports, and agent context — not operating-system access. A
runner still has the permissions of the user who launched it. Proving it cannot
reach another path requires a container or stronger sandbox.

## Installed-wheel smoke test

```sh
scripts/smoke-local.sh
```

The automated counterpart to the rig above, and the same script GitHub Actions
runs on a clean hosted Mac (`.github/workflows/macos-smoke.yml`). It checks
first-start files, the HTTP server, a port collision, and the message shown when
the Claude CLI is absent. It is deliberately non-interactive — use the test rig
when you want to click through the product yourself.

`smoke-local.sh` rebuilds and reinstalls before asserting, then records the
commit it built from in `dist/BUILD_SHA`. `scripts/smoke-installed.sh`, which
holds the assertions, only ever tests the interpreter's *current* install, so
call it directly only when you know what is installed; it refuses to run against
a stamp that disagrees with the checkout. The version string cannot stand in for
this: Superboard's number travels with the code it is projected from, so several
commits legitimately carry the same one.

The installed smoke changes into its fresh workspace before importing Superboard
and rejects any module path inside the checkout. This prevents the repository's
source package from shadowing the wheel on Python's current-directory import path.

## Private-content gate

Install the repo hooks once per clone:

```sh
sh scripts/install-hooks.sh
```

Run the same checks directly when needed:

```sh
python3 scripts/leak_scan.py
python3 scripts/leak_scan.py --history
```

Pre-commit scans the working tree. Pre-push scans the exact outgoing history,
because deleting private content in a later commit does not remove it from older
blobs. CI repeats both checks. Generic patterns cover home paths, email addresses,
private networks, and credential shapes; local vocabulary belongs in the
gitignored `.leakpatterns` file, never in the public repository.
