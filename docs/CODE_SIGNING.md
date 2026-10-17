# Code Signing

## Current status

Boom's release installers (`.msi`, `.exe`, `.AppImage`, `.dmg`) are currently
**unsigned** — there is no trusted publisher identity attached to the binaries.
As a result, Windows SmartScreen labels the publisher as **"Unknown publisher"**
and shows a *"Windows protected your PC"* warning when the MSI installer is run.

The reason is practical, not technical: the repository is still private, and free
code-signing programs for open source projects (such as SignPath) require a public
repository. Nothing in the build pipeline blocks signing — the builds simply have
not been enrolled in a signing program yet.

## Why SmartScreen warns you

Windows identifies software by its digital signature:

1. **Signed binary** — Windows verifies the signature against trusted root
   certificates and displays the verified publisher name in the install dialog.
2. **Unsigned binary** — there is no identity to verify, so Windows reports the
   publisher as **"Unknown"** and shows the SmartScreen warning as a precaution.

The warning is about **identity, not safety**: it means Windows cannot confirm
*who* published the file. It is not a verdict that the file is malicious.

## Roadmap: signed installers

Once the repository is public, we will enroll in
[SignPath](https://signpath.io/) (free code signing for open source projects):

1. Register the Boom project on SignPath against the public repository.
2. Add a signing step to the release workflow (`.github/workflows/release.yml`)
   so every published MSI/EXE is Authenticode-signed before upload.
3. The SmartScreen "Unknown publisher" warning then goes away, replaced by the
   verified publisher name.

Until then, verify your download with the SHA256 checksums below to make sure it
has not been modified.

## Verifying your download (SHA256)

Every release ships a `SHA256SUMS.txt` file alongside the installers. It lists the
SHA256 hash of each published artifact. Compare it against your downloaded file:

**Windows (PowerShell):**

```powershell
Get-FileHash .\Boom_<version>_x64_en-US.msi -Algorithm SHA256
```

Then check that the printed hash matches the entry for that file in `SHA256SUMS.txt`
from the same release.

**macOS / Linux:**

```bash
# verify every file listed in SHA256SUMS.txt
sha256sum -c SHA256SUMS.txt

# or check a single file
sha256sum Boom_<version>_amd64.AppImage
```

If the hashes match, the file you downloaded is byte-identical to the one published
in the release — it has not been tampered with in transit.
