# Handling antivirus false positives

Until releases are code-signed (see `docs/CODE_SIGNING.md`), an unsigned onefile
PyInstaller exe will occasionally be flagged by Windows Defender/SmartScreen or a
third-party antivirus. These are **false positives** — the app is not malware —
but they still block users. This is how to clear them.

> Once code signing is live, flags drop dramatically and this becomes a rare
> stopgap. Signing is the real fix; the steps below are the free interim measures.

---

## 1. Check the current detection rate

Every CI release now runs a **VirusTotal scan** (see the "Scan with VirusTotal"
step in the workflow) if the `VT_API_KEY` secret is set. The workflow log prints
an analysis URL — open it to see which engines flag `FTC-Whisper.exe`.

To enable it: get a free API key at <https://www.virustotal.com/gui/my-apikey>
and add it as a repo secret named `VT_API_KEY` (Settings → Secrets and variables
→ Actions). No key = the step just skips.

You can also scan any exe manually by dragging it onto
<https://www.virustotal.com>.

---

## 2. Report the false positive to the vendors flagging it

Submit the exe (or its SHA-256, shown on VirusTotal) as a false positive. The two
that matter most for reach:

- **Microsoft Defender / SmartScreen** — the big one on Windows.
  Submit at <https://www.microsoft.com/en-us/wdsi/filesubmission>
  → choose "Software developer" → "Incorrectly detected as malware" (false
  positive). Turnaround is usually a day or two. This also helps SmartScreen
  reputation.

- **Any third-party AV that flagged it** — each has its own false-positive form:
  - Avast/AVG: <https://www.avast.com/false-positive-file-form.php>
  - Bitdefender: <https://www.bitdefender.com/consumer/support/answer/29358/>
  - Kaspersky: <https://opentip.kaspersky.com/>
  - Malwarebytes: <https://www.malwarebytes.com/false-positive>
  - McAfee/Trellix, Norton/Gen, ESET: submit via their researcher/false-positive
    portals (search "<vendor> false positive submission").

Attach the release exe and note it's a legitimate open-source dictation tool.

---

## 3. What the build already does (v1.6.29)

Don't undo these — each one exists to keep the detection rate down:

- **No UPX / no packer** (`upx=False`). Packed exes trigger *more* heuristics.
- **Unpacks outside `%TEMP%`** (`runtime_tmpdir` in `ftc_whisper.spec`). A
  onefile exe unpacks its DLLs before running them; doing that in
  `%TEMP%\_MEIxxxxxx` looks exactly like malware staging, and several products
  block the DLL loads *even after the user allows the exe* — the app is
  permitted but still won't run. It now unpacks to
  `%LOCALAPPDATA%\FTC Whisper\runtime\`, which is both less suspicious and a
  **stable path**: an admin can add one permanent exclusion, which is
  impossible with a random `_MEIxxxxxx` name.
  `app._clean_stale_runtime_dirs()` sweeps folders left behind by a crash.
- **Full version metadata** (`version_info.txt`, including Comments and
  LegalTrademarks). Sparse or blank metadata scores against you.
- **Runs as `asInvoker`** — the app never requests admin.

### If a user is still blocked

Ask them to allow-list the two stable paths rather than the exe alone:

```
%LOCALAPPDATA%\FTC Whisper\
```

That single folder covers the exe, the unpack folder and the model.

---

## 4. What NOT to do

- **Don't add UPX** or any packer — see above.
- **Don't obfuscate** the code to "hide" from AV — that makes detection worse.

---

## 5. Bigger free lever (needs its own planned session)

Switching the build from **onefile → onedir + an Inno Setup installer** removes
the self-extraction step entirely, which is the strongest remaining heuristic
after signing. `runtime_tmpdir` (above) softens that trigger but does not remove
it. onedir changes the distribution shape from a single exe to an installed
folder, which **breaks the current auto-update swap logic** (`updater.py` copies
a single exe over itself) and the `FTC-Whisper.exe` release-asset contract. Not
a drop-in change — it needs a planned refactor of the updater. Track as a future
task; do not attempt it piecemeal.
