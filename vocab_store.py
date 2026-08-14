"""Per-account storage for custom vocabulary and snippets.

Both libraries live in `config.json` under a dict keyed by the signed-in
account's email — the same shape `window_sizes` uses, and for the same reason:
two people sharing one machine must not see each other's entries, and a mic
setting is a machine choice whereas a word list is a person's.

    config.vocabulary = {"ryan@ftc.co.uk": [entry, ...], ...}
    config.snippets   = {"ryan@ftc.co.uk": [entry, ...], ...}

Local is the source of truth at dictation time — the transcription hot path
only ever reads an in-memory list, never the network. Supabase is a sync
channel on top (see supabase_client), merged by `updated_at` per row, with
soft deletes so a delete on one machine survives a merge with a stale copy of
the other. That is the same tombstone approach history already uses.

Signed out, everything still works: entries are held under the "_local" key
and continue to apply. They are adopted by the first account that signs in on
that machine, so nothing a user typed before signing in is lost.
"""

import uuid
from datetime import datetime, timezone

VOCABULARY = "vocabulary"
SNIPPETS = "snippets"
_KINDS = (VOCABULARY, SNIPPETS)

# Where entries live when nobody is signed in.
LOCAL_KEY = "_local"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def account_key(email) -> str:
    return (email or "").strip().lower() or LOCAL_KEY


def new_entry(**fields) -> dict:
    """A fresh entry with an id and a timestamp. The id is generated locally so
    an entry created offline keeps its identity when it later syncs."""
    entry = {"id": uuid.uuid4().hex, "updated_at": _now(), "deleted": False}
    entry.update(fields)
    return entry


def _bucket(config, kind: str) -> dict:
    if kind not in _KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    store = getattr(config, kind, None)
    if not isinstance(store, dict):
        store = {}
        setattr(config, kind, store)
    return store


def load(config, kind: str, email=None) -> list:
    """Live (non-deleted) entries for this account, newest edit first."""
    return [e for e in load_all(config, kind, email) if not e.get("deleted")]


def load_all(config, kind: str, email=None) -> list:
    """Every entry including tombstones — what sync needs to see."""
    rows = _bucket(config, kind).get(account_key(email))
    if not isinstance(rows, list):
        return []
    return [e for e in rows if isinstance(e, dict)]


def save(config, kind: str, entries, email=None, persist: bool = True) -> None:
    _bucket(config, kind)[account_key(email)] = list(entries or [])
    if persist:
        try:
            config.save_async()
        except Exception as exc:  # a failed disk write must never lose the UI
            print(f"[VocabStore] save_async failed (non-fatal): {exc}")


def upsert(config, kind: str, entry: dict, email=None) -> dict:
    """Insert or replace by id, restamping `updated_at` so the merge can order
    it against whatever the other machine has."""
    entry = dict(entry)
    entry.setdefault("id", uuid.uuid4().hex)
    entry.setdefault("deleted", False)
    entry["updated_at"] = _now()
    rows = [e for e in load_all(config, kind, email) if e.get("id") != entry["id"]]
    rows.append(entry)
    save(config, kind, rows, email)
    return entry


def soft_delete(config, kind: str, entry_id: str, email=None) -> bool:
    """Tombstone rather than drop. A hard delete loses to any machine that
    still holds the row, which is how a deleted entry comes back."""
    rows = load_all(config, kind, email)
    hit = False
    for e in rows:
        if e.get("id") == entry_id and not e.get("deleted"):
            e["deleted"] = True
            e["updated_at"] = _now()
            hit = True
    if hit:
        save(config, kind, rows, email)
    return hit


def merge(local, remote) -> list:
    """Union by id, last write wins on `updated_at`.

    A tombstone is an ordinary row here: if the delete is the newer edit it
    wins, and if the other side edited the entry afterwards the edit wins and
    the entry comes back. That is the intended behaviour — the most recent
    thing the user did is what they meant.
    """
    by_id = {}
    for row in list(local or []) + list(remote or []):
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if not rid:
            continue
        seen = by_id.get(rid)
        if seen is None or str(row.get("updated_at") or "") >= str(
                seen.get("updated_at") or ""):
            by_id[rid] = row
    # ISO-8601 UTC strings sort chronologically, so this is a real ordering.
    return sorted(by_id.values(), key=lambda e: str(e.get("updated_at") or ""))


def adopt_local_entries(config, email) -> int:
    """Move anything created while signed out into the account that just
    signed in. Without this, a user who adds words before signing in watches
    them vanish the moment they do."""
    key = account_key(email)
    if key == LOCAL_KEY:
        return 0
    moved = 0
    for kind in _KINDS:
        bucket = _bucket(config, kind)
        orphans = bucket.get(LOCAL_KEY) or []
        if not orphans:
            continue
        bucket[key] = merge(bucket.get(key) or [], orphans)
        bucket[LOCAL_KEY] = []
        moved += len(orphans)
    if moved:
        try:
            config.save_async()
        except Exception as exc:
            print(f"[VocabStore] adopt save failed (non-fatal): {exc}")
    return moved


def migrate_legacy_vocabulary(config, email=None) -> int:
    """Convert the old comma-separated `custom_vocabulary` string into entries.

    Runs once per account: the legacy string is LEFT IN PLACE so a downgrade
    still finds it, and terms already present are skipped, so a repeat call
    cannot duplicate them.
    """
    raw = (getattr(config, "custom_vocabulary", "") or "").strip()
    if not raw:
        return 0
    existing = {(e.get("term") or "").strip().lower()
                for e in load_all(config, VOCABULARY, email)}
    rows = load_all(config, VOCABULARY, email)
    added = 0
    for term in raw.split(","):
        term = " ".join(term.split())
        if not term or term.lower() in existing:
            continue
        existing.add(term.lower())
        rows.append(new_entry(term=term, sounds_like=[]))
        added += 1
    if added:
        save(config, VOCABULARY, rows, email)
    return added
