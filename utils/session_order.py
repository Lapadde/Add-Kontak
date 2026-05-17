"""Persistensi urutan kemunculan pertama session (insertion order).

Konsep:
- File JSON `session_order.json` menyimpan list nomor session sesuai urutan
  pertama kali bot melihat file `.session`-nya.
- Setiap pemanggilan `get_ordered_session_phones()` melakukan **sinkronisasi**
  dengan isi `SESSION_DIR` saat ini:
    1. Hapus nomor yang file `.session`-nya sudah tidak ada.
    2. Tambahkan nomor baru di **akhir**. Untuk seeding awal & saat beberapa
       session baru muncul bersamaan, urutkan berdasar **waktu pembuatan file
       `.session`** (terlama dulu, terbaru di akhir) supaya session yang
       baru login selalu jadi nomor paling akhir.
- Akibatnya: nomor urut session lama tidak pernah bergeser kecuali session itu
  sendiri yang dihapus.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Iterable


def _order_file_path() -> str:
    from config import SESSION_DIR

    base = os.path.dirname(os.path.abspath(SESSION_DIR))
    return os.path.join(base, "session_order.json")


def _list_disk_phones() -> list:
    """Nomor dari file .session yang **ada di disk** saat ini (tanpa urut)."""
    from config import SESSION_DIR

    phones = []
    if os.path.exists(SESSION_DIR):
        for name in os.listdir(SESSION_DIR):
            if name.endswith(".session") and not name.endswith("-journal"):
                phones.append(name.replace(".session", ""))
    return phones


def _file_creation_time(phone: str) -> float:
    """Waktu pembuatan file .session (Windows: creation time; Unix: ctime).

    Dipakai untuk mengurutkan session "baru muncul" agar yang baru login muncul
    paling akhir di daftar.
    """
    from config import SESSION_DIR

    candidates = (
        os.path.join(SESSION_DIR, f"{phone}.session"),
        os.path.join(SESSION_DIR, f"{phone}.session-journal"),
    )
    times = []
    for path in candidates:
        try:
            st = os.stat(path)
            t = getattr(st, "st_birthtime", None)
            if t is None:
                t = st.st_ctime
            times.append(float(t))
        except OSError:
            continue
    if not times:
        return 0.0
    # Pakai paling kecil → mendekati waktu pembuatan asli (mtime bisa berubah-ubah
    # karena Telethon menulis ulang session saat update cache).
    return min(times)


def _sort_new_phones_chronological(new_phones: list) -> list:
    """Urutkan nomor baru berdasar waktu pembuatan file (terlama → terbaru).

    Tie-breaker: alfabetis untuk konsistensi.
    """
    return sorted(new_phones, key=lambda p: (_file_creation_time(p), p))


def _load_order_raw() -> list:
    path = _order_file_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            order = data.get("order", [])
        else:
            order = data
        if not isinstance(order, list):
            return []
        # Pastikan string & non-empty
        out = []
        seen = set()
        for p in order:
            if not isinstance(p, str):
                continue
            p = p.strip()
            if not p or p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out
    except Exception:
        return []


def _save_order_atomic(order: list) -> None:
    path = _order_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"order": list(order)}
    # Tulis ke file sementara di folder yang sama, lalu rename (atomic).
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".session_order_", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def _sync(order: list, disk_phones: Iterable[str]) -> tuple[list, bool]:
    """Selaraskan `order` dengan `disk_phones`. Return (new_order, changed)."""
    disk_set = set(disk_phones)
    new_order = [p for p in order if p in disk_set]
    known = set(new_order)
    new_phones = [p for p in disk_phones if p not in known]
    if new_phones:
        new_order.extend(_sort_new_phones_chronological(new_phones))
    changed = new_order != list(order)
    return new_order, changed


def get_ordered_session_phones() -> list:
    """List nomor session sesuai **urutan kemunculan pertama** di bot.

    Sinkronisasi otomatis dengan disk:
    - Nomor baru muncul → di-append di akhir.
    - File hilang → dihapus dari urutan.
    """
    disk_phones = _list_disk_phones()
    if not disk_phones:
        return []
    order = _load_order_raw()
    new_order, changed = _sync(order, disk_phones)
    if changed:
        try:
            _save_order_atomic(new_order)
        except Exception:
            # Best effort; tetap kembalikan urutan tersinkron.
            pass
    return new_order


def session_index(phone: str) -> int:
    """Nomor urut 1-based dari sebuah session, atau 0 jika tidak ada."""
    if not phone:
        return 0
    ordered = get_ordered_session_phones()
    try:
        return ordered.index(phone) + 1
    except ValueError:
        return 0


def forget_session(phone: str) -> None:
    """Hapus nomor dari `session_order.json` (mis. setelah hapus session)."""
    if not phone:
        return
    order = _load_order_raw()
    if phone in order:
        order = [p for p in order if p != phone]
        try:
            _save_order_atomic(order)
        except Exception:
            pass
