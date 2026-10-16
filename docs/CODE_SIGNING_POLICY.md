# Boom Code Signing Policy

> **Template — required by SignPath for the free open-source code signing tier.**
> Fill in the bracketed placeholders once the SignPath Foundation project
> application is approved. (The Boom repository is currently private, so
> registration is pending; see §8.)

## 1. Purpose

This policy defines what the Boom project signs with SignPath, who may
trigger signing, and what review a release must pass before it is signed. It
exists to satisfy the SignPath Foundation's requirement for a documented
signing policy and to make sure a Boom signature always means "this installer
was built by our CI from reviewed source".

## 2. Scope

**Signed:**

- The Windows installer (`.msi` bundle, plus any `.exe` sidecar shipped with
  it) produced by `.github/workflows/desktop.yml` for official Boom Desktop
  releases.

**Explicitly out of scope:**

- Linux AppImage and macOS DMG builds (handled separately).
- Development, pull-request, and ad-hoc builds.
- Any third-party binary not built by the Boom CI pipeline.
- Re-signing artifacts that were not produced by the same workflow run.

Signing changes the installer bytes. The release pipeline always publishes
the SignPath-signed MSI — never the unsigned build — once signing is enabled.

## 3. Who may request signing

- Signing requests are submitted **exclusively by CI**. The SignPath step in
  `desktop.yml` runs only on the Windows build leg and only when the
  `SIGNPATH_*` secrets are configured.
- No manual signing from developer workstations. Maintainers never handle
  private keys — keys are held by SignPath; the CI only holds an API token.
- The `SIGNPATH_API_TOKEN` secret is managed by repository admins
  ([TheOwlOps] org owners). Only maintainers with admin access can add,
  rotate, or revoke it.
- Maintainers: [TODO — link the TheOwlOps org team / CODEOWNERS].

## 4. Review process before a signed release

A Windows installer is eligible for signing only if **all** of the following
hold:

1. **Code review** — every change in the release was merged via pull request
   with at least one maintainer approval. Direct pushes to release branches
   are not permitted.
2. **CI green** — the full `ci.yml` pipeline passed on the exact commit being
   released.
3. **Release tag** — the release is cut from a `desktop-v*` tag created by a
   maintainer (or an explicit `workflow_dispatch` release build with a valid
   `X.Y.Z` version).
4. **Provenance** — the signed artifact comes from the same `desktop.yml`
   workflow run that built it (SignPath signs by GitHub artifact ID; the
   release job prefers the `-signed` artifact and falls back to unsigned only
   when signing was never configured).
5. **No silent downgrade** — if signing is enabled and the SignPath step
   fails, the release fails with it (`wait-for-completion: true`). An
   unsigned installer is never substituted silently.

## 5. Key custody and secret handling

- Code-signing private keys never leave SignPath infrastructure.
- The CI stores only `SIGNPATH_API_TOKEN` as a GitHub Actions secret.
  Non-secret identifiers (`SIGNPATH_ORGANIZATION_ID`,
  `SIGNPATH_PROJECT_SLUG`, `SIGNPATH_SIGNING_POLICY_SLUG`) may alternatively
  be stored as repository variables once assigned.
- Token rotation: [TODO — e.g. rotate annually or on maintainer offboarding].
- If a token is suspected compromised: revoke it in the SignPath dashboard
  immediately, rotate the GitHub secret, and re-run the release.

## 6. Incident response

1. Suspected compromise of a signed release → revoke the signing request in
   the SignPath dashboard and rotate `SIGNPATH_API_TOKEN`.
2. Publish a new patch release with a clean build; never silently replace a
   published installer — issue a new version.
3. Disclose in the release notes what happened and which versions are
   affected.
4. Post-mortem recorded in `docs/` within 7 days.

## 7. SmartScreen note

Authenticode signing plus a trusted timestamp is the complete technical
story on Windows — there is no notarization step as on macOS. SmartScreen
**reputation** is a separate axis: it accrues over time as the signed
installer is downloaded and installed without incident. Early releases may
still show a SmartScreen warning despite a valid signature; this is expected
and resolves as reputation builds.

## 8. Enabling signing (checklist)

Registration is blocked until the Boom repository is public.

1. Make the repository public.
2. Apply to the SignPath Foundation (free for open source) and get the
   project approved.
3. Record the assigned identifiers:
   - `SIGNPATH_ORGANIZATION_ID` — [TODO: GUID from SignPath]
   - `SIGNPATH_PROJECT_SLUG` — [TODO: e.g. `boom`]
   - `SIGNPATH_SIGNING_POLICY_SLUG` — [TODO: e.g. `release-signing`]
   - Optional: `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG` — [TODO]
4. Add them as repository secrets (repo → Settings → Secrets and variables →
   Actions). `SIGNPATH_API_TOKEN` must be a user API token with permission to
   submit signing requests.
5. Configure the SignPath signing policy for automatic approval of CI
   requests (the workflow blocks on `wait-for-completion: true`).
6. Push a `desktop-v*` test tag and verify:
   - the "Sign Windows installer (SignPath)" step runs on the Windows leg,
   - the release contains the signed MSI (check the step log line
     "Shipping SignPath-signed MSI"),
   - `Get-AuthenticodeSignature` on the downloaded MSI reports `Valid`.
7. Without the secrets configured, the same tag must still produce a normal
   unsigned release (signing steps skip silently).

## 9. Audit

- SignPath dashboard audit log records every signing request (who/what/when).
- GitHub Actions run logs record the artifact IDs submitted for signing.
- Release provenance: signed installers are traceable from the GitHub release
  back to the `desktop.yml` run and the `desktop-v*` tag that triggered it.
