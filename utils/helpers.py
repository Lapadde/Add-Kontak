"""Helper functions untuk bot"""
import re
from config import ADMIN_USER_IDS


def validate_phone(phone: str) -> bool:
    """Validasi format nomor telepon"""
    # Hapus spasi dan karakter non-digit
    phone = re.sub(r'\D', '', phone)
    # Cek apakah minimal 10 digit
    return len(phone) >= 10


def is_admin(user_id: int) -> bool:
    """Cek apakah user adalah admin"""
    if not ADMIN_USER_IDS:
        # Jika ADMIN_USER_IDS kosong, semua user bisa akses (backward compatibility)
        return True
    return user_id in ADMIN_USER_IDS

