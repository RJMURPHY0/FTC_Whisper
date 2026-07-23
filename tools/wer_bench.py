"""WER benchmark harness — replay stored dictation WAVs through Parakeet
engine versions and score the results.

Data comes from what the app already keeps on this machine:
  audio      %APPDATA%\\FTC Whisper\\audio\\<created_at digits>.wav
  reference  %APPDATA%\\FTC Whisper\\history.json (transcribed_text per row)

The reference is the shipped engine's own accepted output, so WER against it
is a consistency score, not ground truth. Engine-vs-engine decisions come from
the pairwise section: clips where the versions disagree, optionally settled by
a cheap OpenRouter judge (--judge; paid endpoint, business-data policy).

Usage (venv python, from the project root):
  venv\\Scripts\\python.exe tools\\wer_bench.py                      # v2 only
  venv\\Scripts\\python.exe tools\\wer_bench.py --versions v2 v3     # compare
  venv\\Scripts\\python.exe tools\\wer_bench.py --versions v2 v3 --judge
  venv\\Scripts\\python.exe tools\\wer_bench.py --limit 10           # quick run
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_store  # noqa: E402
from asr_engine import (  # noqa: E402
    ParakeetTranscriber, download_model, model_files_present,
)

JUDGE_MODEL = "deepseek/deepseek-chat"


def norm_words(text: str) -> list[str]:
    return [w.strip(".,!?;:\"'()").lower()
            for w in (text or "").split() if w.strip(".,!?;:\"'()")]


def wer(ref: list[str], hyp: list[str]) -> float:
    """Word error rate via Levenshtein distance. 0.0 = identical."""
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (0 if r == h else 1))
        prev = cur
    return prev[-1] / len(ref)


def load_references() -> dict[str, str]:
    path = os.path.join(os.environ.get("APPDATA", ""), "FTC Whisper",
                        "history.json")
    refs: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for row in json.load(f):
                key = re.sub(r"[^0-9]", "", row.get("created_at") or "")[:20]
                text = (row.get("transcribed_text") or "").strip()
                if key and text:
                    refs[key] = text
    except Exception as e:
        print(f"[bench] No usable history.json ({e}) — pairwise scores only.")
    return refs


def collect_clips(limit: int) -> list[tuple[str, str]]:
    d = audio_store.audio_dir()
    clips = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        names = []
    for name in names:
        if name.endswith(".wav"):
            clips.append((name[:-4], os.path.join(d, name)))
        if limit and len(clips) >= limit:
            break
    return clips


def load_engine(version: str) -> ParakeetTranscriber:
    if not model_files_present(version=version):
        print(f"[bench] Downloading parakeet {version} (~660 MB, one-time)…")
        last = [-10]

        def _progress(frac, msg):
            pct = int(frac * 100)
            if pct - last[0] >= 10:
                last[0] = pct
                print(f"[bench] {msg} ({pct}%)")

        if not download_model(progress=_progress, version=version):
            raise SystemExit(f"[bench] {version} download failed")
    eng = ParakeetTranscriber(auto_punctuate=True, cpu_threads=8,
                              vad_gate=False, model_version=version)
    if not eng.load_model():
        raise SystemExit(f"[bench] {version} failed to load")
    return eng


def judge_pair(text_a: str, text_b: str, va: str, vb: str) -> tuple[str, dict]:
    """Ask a cheap OpenRouter model which transcript reads correct.
    Returns (winner version or 'tie', usage dict)."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return "no-key", {}
    prompt = (
        "Two speech-to-text systems transcribed the same short dictation. "
        "Answer with exactly A, B, or TIE: which transcript is more likely "
        "an accurate transcription of natural spoken English (grammar, "
        "plausible wording, no dropped or invented words)?\n\n"
        f"A: {text_a}\n\nB: {text_b}"
    )
    body = json.dumps({
        "model": JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 5,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())
        answer = (data["choices"][0]["message"]["content"] or "").strip().upper()
        usage = data.get("usage") or {}
        if answer.startswith("A"):
            return va, usage
        if answer.startswith("B"):
            return vb, usage
        return "tie", usage
    except Exception as e:
        print(f"[bench] judge call failed: {e}")
        return "error", {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", nargs="+", default=["v2"])
    ap.add_argument("--limit", type=int, default=0, help="max clips (0 = all)")
    ap.add_argument("--judge", action="store_true",
                    help="LLM-judge divergent pairs (needs OPENROUTER_API_KEY)")
    args = ap.parse_args()

    refs = load_references()
    clips = collect_clips(args.limit)
    if not clips:
        raise SystemExit("[bench] no stored WAVs found")
    print(f"[bench] {len(clips)} clips, {len(refs)} reference transcripts, "
          f"versions: {', '.join(args.versions)}")

    results: dict[str, dict[str, str]] = {v: {} for v in args.versions}
    times: dict[str, float] = {}
    for ver in args.versions:
        eng = load_engine(ver)
        t0 = time.time()
        for key, path in clips:
            audio, rate = audio_store.read(path)
            results[ver][key] = eng.transcribe(audio, rate).strip()
        times[ver] = time.time() - t0
        del eng

    # Per-version consistency vs the history reference
    for ver in args.versions:
        scored = []
        for key, _ in clips:
            if key in refs and results[ver].get(key):
                scored.append(wer(norm_words(refs[key]),
                                  norm_words(results[ver][key])))
        if scored:
            print(f"\n[bench] {ver}: mean WER vs history reference "
                  f"{sum(scored) / len(scored):.3f} over {len(scored)} clips "
                  f"(transcribe time {times[ver]:.0f}s)")

    # Pairwise divergence + optional judge
    if len(args.versions) == 2:
        va, vb = args.versions
        wins = {va: 0, vb: 0, "tie": 0}
        judged = 0
        total_tokens = 0
        print(f"\n[bench] divergent clips ({va} vs {vb}):")
        for key, _ in clips:
            ta, tb = results[va].get(key, ""), results[vb].get(key, "")
            if not ta or not tb:
                continue
            d = wer(norm_words(ta), norm_words(tb))
            if d < 0.05:
                continue
            print(f"  {key}  divergence {d:.2f}")
            print(f"    {va}: {ta[:110]}")
            print(f"    {vb}: {tb[:110]}")
            if args.judge:
                winner, usage = judge_pair(ta, tb, va, vb)
                judged += 1
                total_tokens += int(usage.get("total_tokens") or 0)
                if winner in wins:
                    wins[winner] += 1
                print(f"    judge: {winner}")
        if args.judge and judged:
            print(f"\n[bench] judge verdicts over {judged} divergent clips: "
                  f"{va}={wins[va]}  {vb}={wins[vb]}  tie={wins['tie']}  "
                  f"({JUDGE_MODEL}, ~{total_tokens} tokens)")


if __name__ == "__main__":
    main()
