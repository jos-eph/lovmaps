# Dry-Run Validation Report

> Generated per `CI_CD_RELEASE_PLAN.md` §8.5 (prompt 8.5). No GitHub Actions
> minutes were consumed; no workflow runs were triggered; nothing was pushed.

- **Repo:** `/home/joe/lovmaps`
- **Branch:** `001/initial_setup_claude`
- **Validated commit:** `c9ffacb04e1075b83cdc2971d0374c5e0695aa5b`
- **Validated on (UTC):** 2026-05-26
- **Validator:** Claude Code (Opus 4.7)

## Summary

| # | Check | Status |
|---|-------|--------|
| 1 | `python -m py_compile generate_tiles_pb.py` | PASS |
| 2 | `python generate_tiles_pb.py --help` lists all new flags | PASS |
| 3 | `actionlint` on `.github/workflows/release-tiles.yml` | PASS |
| 4 | Every `uses:` references a 40-character commit SHA | PASS |
| 5 | Workflow `permissions:` grants only `contents: write` | PASS |
| 6 | No `secrets.` references other than `GITHUB_TOKEN` | PASS |

All six dry-run checks pass. No blockers identified.

## Check 1 — `py_compile`

Command (`python3` on this host; `python` is not on PATH but `python3` is, and
CI uses `actions/setup-python` so this discrepancy is local-only):

```
$ python3 -m py_compile generate_tiles_pb.py && echo "EXIT_OK"
EXIT_OK
```

The script compiles cleanly.

## Check 2 — `--help` lists every new flag

Command:

```
$ python3 generate_tiles_pb.py --help
```

Every flag specified by plan §4.1 / prompt 8.1 is present in the help output:

- `--source-pbf SOURCE_PBF` (no default — `None`)
- `--source-url SOURCE_URL` (default: the Geofabrik us-northeast URL from §1.1)
- `--bbox BBOX` (default: `-76.00,39.60,-74.60,40.40`)
- `--base-name BASE_NAME` (default: `philly_commute_region`)
- `--date DATE` (default: auto-generated UTC ISO 8601 `YYYY-MM-DDTHH-MM-SSZ`)
- `--output-dir OUTPUT_DIR` (default: `.`)
- `--keep-intermediates` / `--no-keep-intermediates` (default: keep off)

Note on D3: the help text states the default `--date` format is
`YYYY-MM-DDTHH-MM-SSZ` (naturally sortable ISO 8601 with `-` in place of `:` so
it is filename-safe), matching the human reviewer's revised D3 ("naturally
sortable ISO, please") and the workflow's `date -u +'%Y-%m-%dT%H-%M-%SZ'`
expression at `release-tiles.yml:101`.

## Check 3 — `actionlint`

`actionlint` was not pre-installed. It was fetched from the upstream release
page using the project-published installer script
(`https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash`)
into `/tmp/actionlint` (binary only; no system-wide install, no permanent
state change). Version: `v1.7.12` (linux/arm64).

Command:

```
$ /tmp/actionlint /home/joe/lovmaps/.github/workflows/release-tiles.yml
$ echo "EXIT=$?"
EXIT=0
```

`actionlint` produced no findings.

## Check 4 — `uses:` SHA pinning

All three `uses:` references are pinned to 40-character commit SHAs with a
trailing `# vX.Y.Z` version comment, per plan §4.2 / §6:

| Line | Action | SHA (40 chars) | Comment |
|------|--------|----------------|---------|
| 54 | `actions/checkout` | `de0fac2e4500dabe0009e67214ff5f5447ce83dd` | `# v6.0.2` |
| 57 | `actions/setup-python` | `a309ff8b426b58ec0e2a45f0f869d46889d02405` | `# v6.2.0` |
| 73 | `actions/cache` | `27d5ce7f107fe9357f9df03efb73ab90386fccae` | `# v5.0.5` |

Length of each SHA was independently confirmed to be exactly 40 characters.
Grep for any `uses:` line not matching `@[0-9a-f]{40}` returned no matches.

## Check 5 — `permissions:` block

`release-tiles.yml:31-32`:

```yaml
permissions:
  contents: write   # required to create releases and upload assets
```

This is the only `permissions:` block in the file. It grants exactly one
scope — `contents: write` — required to create releases and upload assets.
No other scopes are granted (no `actions:`, `packages:`, `id-token:`, etc.).
This matches plan §4.2 step 3 and §6 (least privilege).

## Check 6 — `secrets.` references

All four `secrets.` references in the workflow are to the built-in
`secrets.GITHUB_TOKEN`. No custom secrets are referenced.

| Line | Reference |
|------|-----------|
| 135 | `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` |
| 262 | `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` |
| 303 | `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` |
| 334 | `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` |

These are scoped to the steps that invoke `gh release` (existence check,
dated-release create, current-release upload, prune) and nowhere else.

## Notes / Observations (non-blocking)

- **`python` vs `python3`:** the host used for validation only has `python3`
  on PATH. The plan's prompts use bare `python`, which is correct for the
  CI runner (`actions/setup-python` puts both on PATH). Locally, contributors
  may need to invoke `python3` explicitly. This does not affect CI behavior.
- **`actionlint` was downloaded into `/tmp` (not installed system-wide)** so
  no permanent state was changed on this host. The binary will disappear on
  reboot. If recurring local lint runs are desirable, consider documenting
  the installer command (or adding it as a dev-dependency).
- **No `set-output` (deprecated) usage** — the workflow uses `$GITHUB_OUTPUT`
  and `$GITHUB_ENV` exclusively (e.g. `release-tiles.yml:102-103`,
  `release-tiles.yml:129`).
- **Kill switch (plan §11):** the workflow correctly gates all publish steps
  on `vars.RELEASE_PIPELINE_ENABLED == 'true'` (`release-tiles.yml:106-130`).
  Until `@jos-eph` sets this repo variable, daily scheduled runs will build
  and then skip all uploads — visible as a `::notice::` in the run log.

## What was NOT done

Per the prompt, this validation explicitly did NOT:

- Trigger the workflow on GitHub (no Actions minutes consumed).
- Push any commit to the remote (no `git push`).
- Run the script against real OSM data (no network I/O against Geofabrik).
- Modify any tracked source file. Only this report and a single
  pyc artifact under `__pycache__/` were produced; `__pycache__/` is in
  `.gitignore` and is not committed.
