# Security Setup Checklist

> **Audience:** `@jos-eph` only. These are settings that live in the GitHub UI / repo configuration, not in code. An agent cannot apply them on your behalf. Walk through this list once after the CI/CD workflow lands on `main`; revisit if GitHub reorganises its settings UI.
>
> Each item gives: the **UI path**, the **value to set**, and a one-sentence **rationale**. Items are grouped to match §6 of `CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md`.

---

## 1. Branch protection (on `main`)

Apply via **Settings → Branches → Branch protection rules → Add rule** (or **Settings → Rules → Rulesets** if you prefer the newer Rulesets UI; the substance is the same).

- [ ] **Branch name pattern:** `main`
  - *Rationale:* Scope the ruleset to the only branch agents are forbidden to touch directly.
- [ ] **Require a pull request before merging:** ON
  - *Rationale:* Forces every change through review, matching the CLAUDE.md rule that only `@jos-eph` may merge.
- [ ] **Require approvals:** ON, minimum **1** approving review
  - *Rationale:* Ensures a human signs off on every merge even if the PR was opened by the maintainer.
- [ ] **Dismiss stale pull request approvals when new commits are pushed:** ON
  - *Rationale:* Prevents an old approval from rubber-stamping new (possibly unreviewed) commits added to the PR.
- [ ] **Require status checks to pass before merging:** ON (leave the list empty until the workflow exists; revisit after the first successful CI run to add the workflow check)
  - *Rationale:* Once CI exists, no PR can merge with a red build.
- [ ] **Require branches to be up to date before merging:** ON
  - *Rationale:* Forces conflicts and stale-base failures to surface before merge, not after.
- [ ] **Require signed commits:** ON (recommended)
  - *Rationale:* Cryptographically ties each commit to its author and blocks unsigned commits from sneaking in via web edits or compromised tokens.
- [ ] **Require linear history:** ON (recommended)
  - *Rationale:* Disallows merge commits in `main` and keeps `git log` readable and bisectable.
- [ ] **Restrict who can push to matching branches:** ON, allow only `jos-eph`
  - *Rationale:* Enforces the CLAUDE.md rule that only `@jos-eph` may push to `main`.
- [ ] **Allow force pushes:** OFF (disable for everyone, including admins)
  - *Rationale:* Force-pushes to `main` would rewrite history and erase merged work.
- [ ] **Allow deletions:** OFF
  - *Rationale:* `main` should never be deletable; aligns with CLAUDE.md's branch-retention rule.
- [ ] **Do not allow bypassing the above settings:** ON (do not add bypass actors)
  - *Rationale:* If admins can bypass, the protections are advisory rather than enforced.

---

## 2. Actions settings

Apply via **Settings → Actions → General**.

- [ ] **Actions permissions:** *Allow `jos-eph`, and select non-`jos-eph`, actions and reusable workflows*
  - *Rationale:* Lets you whitelist exactly the third-party actions you use (see next item) instead of trusting all of GitHub Marketplace.
- [ ] **Allow actions created by GitHub:** ON
  - *Rationale:* First-party actions (`actions/checkout`, `actions/setup-python`, `actions/cache`) are needed by the release workflow and are the lowest-risk third party available.
- [ ] **Allow actions by Marketplace verified creators:** OFF
  - *Rationale:* Tighter than the GitHub default; we explicitly list every non-GitHub action below.
- [ ] **Allow specified actions and reusable workflows:** Add only the actions referenced by `.github/workflows/release-tiles.yml` (and pin each to a full commit SHA in the workflow file itself, e.g. `softprops/action-gh-release@<40-char-SHA>`).
  - *Rationale:* Least-privilege allowlist — an attacker who publishes a malicious new action in the Marketplace cannot get it executed here.
- [ ] **Fork pull request workflows from outside collaborators:** *Require approval for all outside collaborators*
  - *Rationale:* Stops a drive-by PR from a fork from running arbitrary code in our runner before you have read the diff.
- [ ] **Workflow permissions:** *Read repository contents and packages permissions* (default permissions for `GITHUB_TOKEN`)
  - *Rationale:* The default token grants only read; the workflow file itself opts into `contents: write` for the release step. No other scopes are ever needed.
- [ ] **Allow GitHub Actions to create and approve pull requests:** OFF
  - *Rationale:* Nothing in this repo legitimately needs Actions to open or approve PRs; disabling closes a known privilege-escalation path.

---

## 3. Repository hardening

Apply via **Settings → Code security** (formerly **Code security and analysis**).

- [ ] **Dependabot alerts:** ON
  - *Rationale:* Surfaces known vulnerabilities in any pinned dependency (e.g. a pinned action SHA whose upstream has since been compromised).
- [ ] **Dependabot security updates:** ON
  - *Rationale:* Auto-opens PRs to bump vulnerable dependencies; you still review and merge.
- [ ] **Dependabot version updates:** OFF (leave for now)
  - *Rationale:* This repo's only pinned dependencies are GitHub Actions; chatty version-bump PRs are not worth the noise on a single-maintainer free-tier repo.
- [ ] **Secret scanning:** ON (free for public repos)
  - *Rationale:* Catches accidentally committed credentials before an attacker can.
- [ ] **Secret scanning — Push protection:** ON
  - *Rationale:* Refuses pushes that contain detected secrets, so leaks are blocked at the door instead of cleaned up after the fact.
- [ ] **Code scanning (CodeQL) — Default setup:** ON, language **Python**, schedule **weekly**
  - *Rationale:* A weekly CodeQL run on a single small Python file is well inside the free tier and catches common Python vulnerability patterns.
- [ ] **Private vulnerability reporting:** ON
  - *Rationale:* Gives reporters a way to disclose privately through the GitHub UI in addition to the email in `SECURITY.md`.

Apply via **Settings → General → Features**.

- [ ] **Wikis, Projects, Discussions:** OFF unless actively used
  - *Rationale:* Each enabled surface is another place where someone could post or store content; turn off what you don't use.

Apply via **Settings → Collaborators and teams**.

- [ ] **Collaborators:** only `jos-eph`
  - *Rationale:* Matches the single-maintainer model assumed by CLAUDE.md and the rest of this checklist.

---

## 4. Release hygiene

These items have no UI toggle; they are workflow behaviours enforced in `.github/workflows/release-tiles.yml` and `generate_tiles_pb.py`. Verify by spot-check after the first successful run.

- [ ] Every release asset has a matching `.sha256` sidecar in `sha256sum` format.
  - *Rationale:* Lets any consumer run `sha256sum -c <file>.sha256` to verify the download was not tampered with in transit.
- [ ] The workflow log prints the sha256 of every uploaded file.
  - *Rationale:* Provides a second, independent witness of each asset's hash, recorded outside the release itself.
- [ ] The workflow log prints the upstream Geofabrik `.md5` of the source PBF.
  - *Rationale:* Records the exact upstream artefact used, so provenance is reconstructable months later.
- [ ] `ATTRIBUTION.txt` (and, if you opted for the "reference" form, `MAP-DATA-LICENSE.md`) is uploaded to every dated release and to the `current` release.
  - *Rationale:* Satisfies ODbL §4.2 attribution requirements for every consumer of the data, regardless of which release they pull.
- [ ] Dated releases are immutable: a re-run for an existing `tiles-<DATE>` tag fails fast.
  - *Rationale:* Guarantees that a consumer who pinned to a date will always see the same bytes.
- [ ] `current` release is the only mutable one and is always `--clobber`'d.
  - *Rationale:* Gives consumers a stable rolling URL without the ambiguity of a moving dated tag.

---

## After applying

Once every box above is ticked, delete nothing — leave this file in the repo so a future maintainer (or future you) can re-audit the settings in five minutes.
