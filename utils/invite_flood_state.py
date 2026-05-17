"""Persistensi status FloodWait undangan kontak per nomor session (file JSON)."""
import json
import os
import time


def _state_file_path() -> str:
    from config import SESSION_DIR

    base = os.path.dirname(os.path.abspath(SESSION_DIR))
    return os.path.join(base, "invite_flood_state.json")


def _normalize_phone(phone: str) -> str:
    return (phone or "").strip()


def _load() -> dict:
    path = _state_file_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    path = _state_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def record_invite_flood(phone: str, flood_seconds: int) -> None:
    """Simpan limit dari Telegram; sisa aktif = now + flood_seconds (estimasi)."""
    phone = _normalize_phone(phone)
    if not phone:
        return
    now = time.time()
    fs = max(0, int(flood_seconds))
    data = _load()
    data[phone] = {
        "flood_seconds": fs,
        "recorded_at": now,
        "until": now + fs,
    }
    _save(data)


def clear_invite_flood(phone: str) -> None:
    """Hapus catatan limit (mis. setelah invite sukses penuh)."""
    phone = _normalize_phone(phone)
    data = _load()
    if phone in data:
        del data[phone]
        _save(data)


def human_duration_seconds(sec: int) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} jam")
    if m:
        parts.append(f"{m} menit")
    if s or not parts:
        parts.append(f"{s} detik")
    return " ".join(parts)


def get_invite_flood_status(phone: str) -> dict:
    """Baca dari disk + waktu sistem (selalu mutakhir saat dipanggil).

    Returns:
        active True: limit undangan dianggap masih berlaku
        kind: 'none' | 'active' | 'expired'
    """
    phone = _normalize_phone(phone)
    entry = _load().get(phone)
    if not entry:
        return {"kind": "none", "active": False}
    now = time.time()
    until = float(entry.get("until", 0))
    fs = int(entry.get("flood_seconds", 0))
    if now < until:
        return {
            "kind": "active",
            "active": True,
            "remaining_sec": int(until - now),
            "flood_seconds": fs,
            "recorded_at": float(entry.get("recorded_at", 0)),
            "until": until,
        }
    return {
        "kind": "expired",
        "active": False,
        "last_flood_seconds": fs,
        "until": until,
    }


def list_mark_for_session(phone: str) -> str:
    """✅ limit tidak aktif / ❌ limit undangan (FloodWait) masih dihitung aktif."""
    st = get_invite_flood_status(phone)
    return "❌" if st.get("active") else "✅"
