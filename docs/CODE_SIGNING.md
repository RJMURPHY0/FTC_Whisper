# Code signing — Azure Trusted Signing

FTC Whisper is signed with **Azure Trusted Signing** so Windows SmartScreen and
Defender trust it immediately (no "Windows protected your PC" wall, far fewer
antivirus false positives). Signing happens automatically in CI
(`.github/workflows/build-release.yml`) — every tagged release is signed before
the `FTC-Whisper.exe` asset is published.

This doc is the **one-time setup** you (the account owner) must do. The build
itself is already wired up; it just needs the six secrets below.

---

## ⚠️ Read this first — the identity gate

Trusted Signing will only issue a certificate profile after Microsoft validates
your identity. **This is the part that can block you, so start it first.**

- **Individual validation:** requires a verifiable identity with **~3 years of
  history** (government ID + records Microsoft can cross-check). Some solo devs
  get rejected here — if so, fall back to an EV/OV cert or the Microsoft Store.
- **Organization validation:** requires the legal business name + a **D-U-N-S
  number** (free to request from Dun & Bradstreet, can take a few days). If FTC
  Safety is a registered company, use org validation — it's more reliable than
  individual and the cert says "FTC Safety" instead of your personal name.

Validation typically takes 1–5 business days. Nothing else works until it clears.

---

## One-time Azure setup

Cost: ~US$9.99/month for the Trusted Signing account (Basic tier). You need an
Azure subscription (pay-as-you-go is fine).

1. **Create the Trusted Signing account**
   - Azure Portal → search **"Trusted Signing"** → **Create**.
   - Pick a resource group + region. Note the region — its endpoint is one of
     your secrets (e.g. East US → `https://eus.codesigning.azure.net/`).
   - Give the account a name → this is `AZURE_CODE_SIGNING_NAME`.

2. **Complete identity validation**
   - In the account → **Identity validations** → start Individual or
     Organization validation. Wait for it to reach **Completed**.

3. **Create a certificate profile**
   - In the account → **Certificate profiles** → **Create**.
   - Type: **Public Trust**. Link it to the completed identity validation.
   - The profile name → this is `AZURE_CERT_PROFILE_NAME`.

4. **Create a service principal for CI** (so GitHub can sign without your login)
   - Azure Portal → **Microsoft Entra ID** → **App registrations** → **New
     registration**. Name it e.g. `ftc-whisper-signing`.
   - After creation, note the **Application (client) ID** → `AZURE_CLIENT_ID`
     and the **Directory (tenant) ID** → `AZURE_TENANT_ID`.
   - **Certificates & secrets** → **New client secret** → copy the *Value*
     immediately → `AZURE_CLIENT_SECRET`.

5. **Grant the service principal signing rights**
   - Trusted Signing account → **Access control (IAM)** → **Add role
     assignment** → role **"Trusted Signing Certificate Profile Signer"** →
     assign to the `ftc-whisper-signing` app registration.

---

## Add the six GitHub secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add all six (names must match exactly):

| Secret | Value |
|---|---|
| `AZURE_TENANT_ID` | Directory (tenant) ID from step 4 |
| `AZURE_CLIENT_ID` | Application (client) ID from step 4 |
| `AZURE_CLIENT_SECRET` | Client secret *Value* from step 4 |
| `AZURE_ENDPOINT` | Region endpoint, e.g. `https://eus.codesigning.azure.net/` |
| `AZURE_CODE_SIGNING_NAME` | Trusted Signing account name (step 1) |
| `AZURE_CERT_PROFILE_NAME` | Certificate profile name (step 3) |

Once these exist, the next `git tag vX.Y.Z && git push --tags` (or the manual
**Run workflow** button) produces a **signed** `FTC-Whisper.exe`. The workflow's
**Verify signature** step fails the build if signing didn't take — so you'll
know immediately, not from a user complaint.

If the secrets are absent (e.g. someone forks the repo), the sign + verify steps
skip automatically and the build still produces an unsigned exe.

---

## Verifying a signed build

On any Windows machine, right-click the exe → **Properties → Digital
Signatures** — you should see your publisher name. Or in PowerShell:

```powershell
Get-AuthenticodeSignature "FTC-Whisper.exe" | Format-List Status, SignerCertificate
```

`Status` must be `Valid`.

## Notes

- Trusted Signing certs are **short-lived** (rotated every few days by Azure) but
  the **timestamp** (`http://timestamp.acs.microsoft.com`) means signed exes stay
  valid forever — this is why the timestamp step is not optional.
- Reputation with SmartScreen is tied to the *certificate identity*, not each
  file hash, so once your first signed release is trusted, every future signed
  release inherits that trust instantly.
