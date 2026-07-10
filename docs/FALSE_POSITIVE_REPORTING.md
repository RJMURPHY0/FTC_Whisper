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

## 3. What NOT to do

- **Don't add UPX** or any packer — already disabled in `ftc_whisper.spec` for
  this exact reason; packed exes trigger *more* AV heuristics, not fewer.
- **Don't obfuscate** the code to "hide" from AV — that makes detection worse.

---

## 4. Bigger free lever (needs its own planned session)

The single most effective *free* reduction in AV false positives is switching the
build from **onefile → onedir + an Inno Setup installer**. The onefile
self-extract-to-temp pattern is the #1 heuristic trigger. However, this changes
the distribution shape from a single exe to an installed folder, which **breaks
the current auto-update swap logic** (`updater.py` copies a single exe over
itself). So it's not a drop-in change — it needs a planned refactor of the
updater. Track this as a future task; do not attempt it piecemeal.
