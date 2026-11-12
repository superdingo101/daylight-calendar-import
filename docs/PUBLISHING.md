# Mintlify release publication (operator runbook)

Daylight Calendar Import has a separate Mintlify site from Daylight Calendar Card.
The site must deploy from the **`docs-live` branch** and the **`/docs` folder**.

## Why a separate branch?

Normal feature PRs merge code and documentation to `main`. HACS distributes
published GitHub releases, not the latest `main`. The public docs therefore
track the *latest published stable release tag*, not development. The
`.github/workflows/publish-mintlify-docs.yml` workflow advances
`docs-live` to exactly the released Git commit only when all validations pass.

The branch is a movable **Git reference to an existing stable release commit**.
It includes the source repository tree at that tag; Mintlify only reads `docs/`.
Do not edit `docs-live` directly, force-push it, or point Mintlify to `main`.

## Initial Mintlify site setup

1. In the existing Mintlify organization, create an additional **Starter** site
   for Daylight Calendar Import. Do not change Card's existing OSS Pro site.
2. Install/authorize the Mintlify GitHub App for only
   `superdingo101/daylight-calendar-import` if not already authorized.
3. Set **Git repository** to `superdingo101/daylight-calendar-import`,
   **production branch** to `docs-live`, and **documentation directory** to
   `/docs` (enable the docs.json-in-subdirectory option).
4. The `docs-live` branch intentionally does **not exist** until the first
   eligible stable release includes the new Mintlify docs. If Mintlify requires
   it during setup, save the site without a Git connection and finish the
   connection after the next qualifying release. Do **not** bootstrap public
   docs from an unreleased development commit.
5. Once the released site deploys successfully on Mintlify's preview URL,
   set custom domain `import.daylightcalendar.com` in Mintlify and enter the
   **exact DNS record(s) Mintlify provides** in Squarespace DNS. Wait for
   verification and HTTPS. Do not invent DNS targets or change Card's site.
6. Link to Import docs from `daylightcalendar.com` and from the Card docs site
   only after the custom domain responds successfully.

## Publishing a release

1. Merge tested documentation and code into `main` under the normal existing
   HACS release gates, including full mutation validation.
2. Publish a **stable** GitHub release from `main`, with a tag such as
   `v0.9.0`, whose manifest and pyproject versions match and whose asset
   includes a nonempty `daylight-calendar-import.zip`.
3. The `release.published` event triggers the publishing workflow:
   verify authoritative release metadata, tag identity and main ancestry,
   docs navigation, required pages, local assets, `mint validate`, and
   `mint broken-links`. Only then does it fast-forward `docs-live`.
4. Verify the branch SHA equals the published release commit and the Mintlify
   site shows the correct released features.

An unpublished draft or beta release cannot update `docs-live`.
An out-of-order release, invalid MDX/navigation, failed Mintlify check,
or Git push permission failure leaves the previous published docs untouched.

## Optional beta documentation rehearsal

Before the first stable release, you can rehearse the publication using an actual
**published beta** (for example `v0.9.0-beta2`). This uses a separate Git branch
named `docs-rehearsal` and **never** updates production `docs-live`.

1. Prepare the beta normally: versions in `manifest.json` and `pyproject.toml`
   must match the exact beta tag. Pass the existing HACS/Hassfest, browser and
   full clean mutation-proof gates. The existing HACS release workflow creates
   a **draft** GitHub release with a ZIP; inspect it and publish it explicitly
   as a **prerelease**. A draft or untagged candidate is insufficient.
2. In GitHub Actions, choose **Rehearse Mintlify docs from published beta**,
   then **Run workflow** from `main`, with `release_tag=v0.9.0-beta2`.
3. The workflow reads GitHub's published release metadata, verifies the
   prerelease flag and nonempty HACS ZIP, checks out the precise tag, validates
   the integration versions and Mintlify site, runs `mint validate` and
   `mint broken-links`, and only then fast-forwards `docs-rehearsal`.
4. Verify that `docs-rehearsal` equals the beta tag's full commit SHA and
   that `docs-live` did not move. Re-run the workflow once to prove idempotency.
5. This is a **Git and documentation-build rehearsal**, not proof of a live
   Mintlify deployment. The public Import site must still be configured to
   use `docs-live` only when a stable release exists. A separate private
   Mintlify preview project can be used to test beta rendering if desired;
   do not switch the public site to the rehearsal branch.

Beta mode rejects stable, draft, release-candidate and malformed beta tags,
and cannot update `docs-live`. Stable mode rejects prereleases and cannot
update `docs-rehearsal`. Both reject out-of-order, divergent or invalid
releases without moving their respective branches.

## Recovery

The workflow also allows **Run workflow** with `release_tag` set to an
*already published stable release*. This rechecks all gates and is idempotent.
It is not a bypass to publish a draft or an arbitrary development commit.

If a documentation-only correction is urgent before the next feature release,
prepare a small **patch release** containing the fix and normal verified HACS
asset. Do not directly patch `docs-live`; otherwise the next release could
discard the edit or violate the release/content consistency guarantee.

The first eligible stable release must include the docs foundation.
Earlier stable tags, such as v0.8.0, do not contain the new docs site and are
deliberately not eligible for this new publishing flow.
