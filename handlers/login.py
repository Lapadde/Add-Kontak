"""Handlers untuk login flow"""
import os
import re
from telegram import Update, ReplyKeyboardRemove
from telegram.helpers import escape_markdown
from telegram.ext import ConversationHandler, CommandHandler, MessageHandler, filters, ContextTypes
from telethon_client import TelethonAuth
from utils.helpers import validate_phone, is_admin
from config import SESSION_DIR
from utils.session_order import get_ordered_session_phones


def _normalize_phone_input(raw: str) -> str:
    """Normalisasi nomor telepon ke format E.164 (`+<kode_negara><nomor>`).

    Aturan (dievaluasi berurutan):
    1. Bila input diawali ``+`` → dianggap sudah berformat internasional, hanya
       karakter non-digit yang dibuang. Cocok untuk semua negara.
    2. Bila input (setelah dibersihkan) diawali ``00`` → prefiks dialing
       internasional umum (Eropa & lainnya). ``00`` dibuang lalu diganti ``+``.
    3. Bila diawali ``0`` tunggal → diasumsikan format lokal Indonesia,
       sehingga ``0`` diganti ``62``.
    4. Selain itu (diawali 1-9) → diasumsikan sudah memuat country code tanpa
       tanda ``+`` (mis. ``14155552671`` untuk US).

    Catatan: aturan #3 spesifik untuk Indonesia. User dari negara lain yang
    nomor lokalnya juga berawalan ``0`` (UK, Jerman, Australia, dll.) harus
    menggunakan format internasional ``+<kode_negara>…``.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+62" + digits[1:]
    return "+" + digits


def _looks_like_valid_e164(phone: str) -> bool:
    """Cek apakah nomor sudah berformat E.164 yang masuk akal.

    Standar E.164: maks 15 digit setelah ``+``. Praktiknya panjang nomor MSISDN
    minimum sekitar 8 digit. Country code valid tidak mungkin diawali ``0``.
    """
    if not phone or not phone.startswith("+"):
        return False
    digits = phone[1:]
    if not digits.isdigit():
        return False
    if not (8 <= len(digits) <= 15):
        return False
    if digits.startswith("0"):
        return False
    return True


def _format_session_saved_path(phone_number: str) -> str:
    """Pesan path session yang disimpan, mengikuti `SESSION_DIR` di config."""
    return os.path.join(SESSION_DIR, f"{phone_number}.session")

# State untuk ConversationHandler
PHONE, OTP, PASSWORD = range(3)

# Dictionary untuk menyimpan auth objects per user (shared state)
# Key: user_id (Telegram user ID), Value: TelethonAuth instance
user_auths = {}

# Timeout untuk cleanup auth objects yang tidak aktif (dalam detik)
AUTH_TIMEOUT = 300  # 5 menit

# Idle timeout untuk ConversationHandler login. Jika admin tidak mengirim
# nomor / OTP / password lebih dari ini, alur login dibatalkan otomatis.
LOGIN_IDLE_TIMEOUT_SECS = 180  # 3 menit


async def cleanup_login_sessions():
    """Membersihkan semua login sessions yang masih aktif saat bot shutdown.
    Dipanggil dari bot.py post_shutdown callback.
    """
    if not user_auths:
        return
    
    print(f"[LOGIN] Cleaning up {len(user_auths)} active login session(s)...")
    for user_id, auth in list(user_auths.items()):
        try:
            await auth.disconnect()
            print(f"[LOGIN] Disconnected login session for user {user_id}")
        except Exception:
            pass
    user_auths.clear()
    print("[LOGIN] All login sessions cleaned up")


async def delete_last_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menghapus pesan terakhir bot untuk user tertentu"""
    try:
        if 'last_bot_message_id' in context.user_data:
            last_msg_id = context.user_data.get('last_bot_message_id')
            if last_msg_id:
                try:
                    chat_id = update.effective_chat.id
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=last_msg_id
                    )
                except Exception:
                    # Ignore error jika pesan sudah dihapus atau tidak ditemukan
                    pass
                context.user_data['last_bot_message_id'] = None
    except Exception:
        # Ignore semua error saat delete message
        pass


async def send_and_save_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Mengirim pesan dan menyimpan message ID, sambil menghapus pesan terakhir"""
    # Hapus pesan terakhir sebelum mengirim pesan baru
    await delete_last_message(update, context)
    
    # Kirim pesan baru
    sent_message = await update.message.reply_text(text, **kwargs)
    
    # Simpan message ID untuk dihapus nanti
    if sent_message:
        context.user_data['last_bot_message_id'] = sent_message.message_id
    
    return sent_message


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk command /start"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    welcome_msg = (
        "🤖 **Bot Login Telegram Userbot**\n\n"
        "Bot ini akan membantu Anda login ke akun Telegram menggunakan Telethon.\n\n"
        "**Cara penggunaan:**\n"
        "1. Kirim nomor telepon Anda dalam format internasional, mis. "
        "`+628123456789` (ID), `+14155552671` (US), `+447911123456` (UK). "
        "Untuk Indonesia, `08123456789` juga diterima.\n"
        "2. Bot akan mengirimkan kode OTP ke Telegram Anda\n"
        "3. Kirim kode OTP yang diterima\n"
        "4. Jika akun Anda menggunakan 2FA, kirim password\n\n"
        "**Perintah:**\n"
        "/login - Mulai proses login\n"
        "/manage - Menu manajemen sessions\n"
        "/resend - Kirim ulang kode OTP\n"
        "/cancel - Batalkan proses login\n\n"
        "Kirim /login untuk memulai!"
    )
    await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    return ConversationHandler.END


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai proses login"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Cek apakah user sudah punya proses login aktif
    if user_id in user_auths:
        await update.message.reply_text(
            "⚠️ Anda sudah memiliki proses login yang aktif. "
            "Gunakan /cancel untuk membatalkan terlebih dahulu."
        )
        return ConversationHandler.END
    
    # Hapus pesan user (nomor telepon yang dikirim)
    try:
        await update.message.delete()
    except Exception:
        pass
    
    await send_and_save_message(
        update, context,
        "📱 **Langkah 1: Nomor Telepon**\n\n"
        "Silakan kirim nomor telepon Anda dalam format internasional.\n"
        "Contoh:\n"
        "• `+628123456789` — Indonesia\n"
        "• `+14155552671` — US\n"
        "• `+447911123456` — UK\n"
        "• `+491701234567` — Jerman\n\n"
        "Khusus Indonesia, format lokal `08123456789` juga diterima.\n\n"
        "Gunakan /cancel untuk membatalkan.",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove()
    )
    return PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima nomor telepon dan meminta OTP"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    phone_number = update.message.text.strip()
    
    # Hapus pesan user (nomor telepon yang dikirim)
    try:
        await update.message.delete()
    except Exception:
        pass
    
    # Validasi minimal panjang (≥10 digit), agnostik negara.
    if not validate_phone(phone_number):
        await send_and_save_message(
            update, context,
            "❌ Format nomor telepon tidak valid!\n\n"
            "Kirim nomor dalam format internasional, contoh:\n"
            "• `+628123456789` (Indonesia)\n"
            "• `+14155552671` (US)\n"
            "• `+447911123456` (UK)\n\n"
            "Khusus Indonesia, `08123456789` juga diterima.",
            parse_mode='Markdown'
        )
        return PHONE

    # Normalisasi ke E.164. Mendukung beragam input:
    # - `+<cc><nomor>` → dipertahankan
    # - `00<cc><nomor>` → `+<cc><nomor>` (dialing prefix internasional)
    # - `0<nomor>` → `+62<nomor>` (lokal Indonesia)
    # - `<cc><nomor>` tanpa `+` → `+<cc><nomor>`
    phone_number = _normalize_phone_input(phone_number)
    if not _looks_like_valid_e164(phone_number):
        await send_and_save_message(
            update, context,
            "❌ Format nomor telepon tidak valid!\n\n"
            "Gunakan format internasional E.164 (`+<kode_negara><nomor>`), "
            "panjang total 8–15 digit setelah tanda `+`.\n\n"
            "Contoh:\n"
            "• `+628123456789` (Indonesia)\n"
            "• `+14155552671` (US)\n"
            "• `+447911123456` (UK)",
            parse_mode='Markdown'
        )
        return PHONE

    try:
        await send_and_save_message(
            update, context,
            "⏳ Memproses nomor telepon...\n"
            "Mohon tunggu sebentar..."
        )

        # Buat TelethonAuth instance
        auth = TelethonAuth(phone_number)
        user_auths[user_id] = auth

        # `connect()` dipanggil sekali. Telethon di dalam `connect()`:
        # - kalau sudah authorized → return True.
        # - kalau belum authorized → otomatis `send_code_request` lalu return False.
        connected = await auth.connect()
        if connected:
            try:
                me = await auth.get_me()
                await send_and_save_message(
                    update, context,
                    f"✅ **Login berhasil!**\n\n"
                    f"👤 Nama: {escape_markdown(me.first_name or 'N/A', version=1)}\n"
                    f"📱 Nomor: {escape_markdown(phone_number, version=1)}\n"
                    f"🆔 ID: `{me.id}`\n\n"
                    f"Session tersimpan di: `{escape_markdown(_format_session_saved_path(phone_number), version=1)}`",
                    parse_mode='Markdown'
                )
            finally:
                try:
                    await auth.disconnect()
                except Exception:
                    pass
            if user_id in user_auths:
                del user_auths[user_id]
            # Pastikan session order ikut tersinkron supaya nomor baru langsung
            # muncul di posisi paling akhir.
            try:
                get_ordered_session_phones()
            except Exception:
                pass
            context.user_data['last_bot_message_id'] = None
            return ConversationHandler.END

        # `connected == False` → OTP sudah dikirim oleh `send_code_request`
        # bagian dari `connect()`. Kalau file session lama ada tapi sudah
        # revoked, beri tahu user terus terang.
        had_stale_session = auth.is_session_exists()
        prompt_lines = [
            "✅ Kode OTP telah dikirim ke Telegram Anda!\n",
        ]
        if had_stale_session:
            prompt_lines.append(
                "ℹ️ Session lama untuk nomor ini ditemukan tetapi **tidak valid lagi**; "
                "perlu otentikasi ulang dengan OTP\\.\n"
            )
        prompt_lines.extend([
            "📱 **Langkah 2: Kode OTP**\n",
            "Silakan kirim kode OTP yang Anda terima.",
            "Format: 12345 (5 digit angka)\n",
            "💡 **Tips:**",
            "- Kode OTP berlaku selama 5 menit",
            "- Jika tidak menerima kode, gunakan /resend",
            "- Gunakan /cancel untuk membatalkan",
        ])
        await send_and_save_message(
            update, context,
            "\n".join(prompt_lines),
            parse_mode='Markdown'
        )
        return OTP

    except Exception as e:
        error_msg = str(e)
        if "all available options" in error_msg.lower() or "resendcode" in error_msg.lower():
            await send_and_save_message(
                update, context,
                "⚠️ **Semua metode verifikasi sudah digunakan**\n\n"
                "Silakan tunggu beberapa saat, lalu gunakan /login lagi untuk meminta kode OTP baru.\n\n"
                "Atau gunakan /cancel untuk membatalkan.",
                parse_mode='Markdown'
            )
        else:
            await send_and_save_message(
                update, context,
                f"❌ Error: {error_msg}\n\n"
                "Silakan coba lagi atau gunakan /cancel untuk membatalkan."
            )
        # Hanya disconnect — JANGAN hapus file session di sini. File session
        # tersimpan hanya setelah sign_in sukses; pada tahap ini biasanya belum
        # ada. Jika file lama dari nomor yang sama kebetulan ada, ia ditangani
        # oleh alur pengulangan login berikutnya, bukan dihapus diam-diam.
        if user_id in user_auths:
            auth = user_auths[user_id]
            try:
                await auth.disconnect()
            except Exception:
                pass
            del user_auths[user_id]
        return ConversationHandler.END


async def receive_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima OTP dan meminta password jika diperlukan"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    otp_code = update.message.text.strip()
    
    # Hapus pesan user (kode OTP yang dikirim)
    try:
        await update.message.delete()
    except Exception:
        pass
    
    if user_id not in user_auths:
        await send_and_save_message(
            update, context,
            "❌ Session tidak ditemukan. Silakan mulai dari /login"
        )
        return ConversationHandler.END
    
    auth = user_auths[user_id]
    
    # Validasi OTP (harus angka)
    if not otp_code.isdigit():
        await send_and_save_message(
            update, context,
            "❌ Kode OTP harus berupa angka!\n\n"
            "Silakan kirim kode OTP yang valid."
        )
        return OTP
    
    try:
        await send_and_save_message(
            update, context,
            "⏳ Memverifikasi kode OTP...\n"
            "Mohon tunggu sebentar..."
        )
        
        success, message = await auth.sign_in(otp_code=otp_code)
        
        if success:
            # Login berhasil
            try:
                me = await auth.get_me()
                await send_and_save_message(
                    update, context,
                    f"✅ **Login berhasil!**\n\n"
                    f"👤 Nama: {escape_markdown(me.first_name or 'N/A', version=1)}\n"
                    f"📱 Nomor: {escape_markdown(auth.phone_number, version=1)}\n"
                    f"🆔 ID: `{me.id}`\n\n"
                    f"Session tersimpan di: `{escape_markdown(_format_session_saved_path(auth.phone_number), version=1)}`",
                    parse_mode='Markdown'
                )
            finally:
                # Pastikan disconnect bahkan jika ada error saat get_me
                try:
                    await auth.disconnect()
                except Exception:
                    pass
            if user_id in user_auths:
                del user_auths[user_id]
            try:
                get_ordered_session_phones()
            except Exception:
                pass
            # Reset last_bot_message_id agar pesan login berhasil tidak dihapus
            context.user_data['last_bot_message_id'] = None
            return ConversationHandler.END
        else:
            if "Password diperlukan" in message or "password" in message.lower():
                await send_and_save_message(
                    update, context,
                    "🔐 **Langkah 3: Password 2FA**\n\n"
                    "Akun Anda menggunakan Two-Factor Authentication (2FA).\n"
                    "Silakan kirim password 2FA Anda.\n\n"
                    "Gunakan /cancel untuk membatalkan.",
                    parse_mode='Markdown'
                )
                return PASSWORD
            elif message == "RESEND_NEEDED":
                await send_and_save_message(
                    update, context,
                    "⚠️ **Semua metode verifikasi sudah digunakan**\n\n"
                    "Silakan gunakan /resend untuk meminta kode OTP baru.\n\n"
                    "Atau gunakan /cancel untuk membatalkan.",
                    parse_mode='Markdown'
                )
                return OTP
            else:
                await send_and_save_message(
                    update, context,
                    f"❌ {escape_markdown(message, version=1)}\n\n"
                    "💡 **Tips:**\n"
                    "- Pastikan kode OTP masih valid (biasanya 5 menit)\n"
                    "- Jika kode sudah kedaluwarsa, gunakan /resend\n"
                    "- Atau gunakan /cancel untuk membatalkan.",
                    parse_mode='Markdown'
                )
                return OTP
                
    except Exception as e:
        await send_and_save_message(
            update, context,
            f"❌ Error: {str(e)}\n\n"
            "Silakan coba lagi atau gunakan /cancel untuk membatalkan."
        )
        # Error saat verifikasi OTP — tetap stay di state OTP. Jangan hapus
        # file session di sini (lihat catatan di receive_phone).
        return OTP


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima password 2FA dan menyelesaikan login"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    password = update.message.text.strip()
    
    # Hapus pesan user (password yang dikirim)
    try:
        await update.message.delete()
    except Exception:
        pass
    
    if user_id not in user_auths:
        await send_and_save_message(
            update, context,
            "❌ Session tidak ditemukan. Silakan mulai dari /login"
        )
        return ConversationHandler.END
    
    auth = user_auths[user_id]
    
    try:
        await send_and_save_message(
            update, context,
            "⏳ Memverifikasi password...\n"
            "Mohon tunggu sebentar..."
        )
        
        success, message = await auth.sign_in(password=password)
        
        if success:
            # Login berhasil
            try:
                me = await auth.get_me()
                await send_and_save_message(
                    update, context,
                    f"✅ **Login berhasil!**\n\n"
                    f"👤 Nama: {escape_markdown(me.first_name or 'N/A', version=1)}\n"
                    f"📱 Nomor: {escape_markdown(auth.phone_number, version=1)}\n"
                    f"🆔 ID: `{me.id}`\n\n"
                    f"Session tersimpan di: `{escape_markdown(_format_session_saved_path(auth.phone_number), version=1)}`",
                    parse_mode='Markdown'
                )
            finally:
                # Pastikan disconnect bahkan jika ada error saat get_me
                try:
                    await auth.disconnect()
                except Exception:
                    pass
            if user_id in user_auths:
                del user_auths[user_id]
            try:
                get_ordered_session_phones()
            except Exception:
                pass
            # Reset last_bot_message_id agar pesan login berhasil tidak dihapus
            context.user_data['last_bot_message_id'] = None
            return ConversationHandler.END
        else:
            await send_and_save_message(
                update, context,
                f"❌ {escape_markdown(message, version=1)}\n\n"
                "Silakan coba lagi atau gunakan /cancel untuk membatalkan.",
                parse_mode='Markdown'
            )
            return PASSWORD

    except Exception as e:
        await send_and_save_message(
            update, context,
            f"❌ Error: {str(e)}\n\n"
            "Silakan coba lagi atau gunakan /cancel untuk membatalkan."
        )
        # Stay di state PASSWORD; jangan menghapus file session di sini.
        return PASSWORD


async def resend_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengirim ulang kode OTP"""
    user_id = update.effective_user.id

    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    if user_id not in user_auths:
        await update.message.reply_text(
            "❌ Session tidak ditemukan. Silakan mulai dari /login"
        )
        return ConversationHandler.END
    
    auth = user_auths[user_id]
    
    try:
        await send_and_save_message(
            update, context,
            "⏳ Mengirim ulang kode OTP...\n"
            "Mohon tunggu sebentar..."
        )
        
        success, message = await auth.resend_code()
        
        if success:
            await send_and_save_message(
                update, context,
                "✅ **Kode OTP baru telah dikirim!**\n\n"
                "📱 Silakan cek Telegram Anda dan kirim kode OTP yang baru.\n\n"
                "Gunakan /cancel untuk membatalkan.",
                parse_mode='Markdown'
            )
            return OTP
        else:
            await send_and_save_message(
                update, context,
                f"❌ {message}\n\n"
                "Silakan coba lagi atau gunakan /cancel untuk membatalkan."
            )
            return OTP
            
    except Exception as e:
        await send_and_save_message(
            update, context,
            f"❌ Error: {str(e)}\n\n"
            "Silakan coba lagi atau gunakan /cancel untuk membatalkan."
        )
        return OTP


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan proses login"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    if user_id in user_auths:
        auth = user_auths[user_id]
        # Tentukan dulu apakah user sudah benar-benar authorized (sign_in sukses).
        # Kalau ya, JANGAN hapus file session — itu data session aktif yang valid.
        # Kalau belum, file session "kosong" yang dibuat saat connect() boleh dibersihkan.
        was_authorized = False
        try:
            if auth.client is not None:
                was_authorized = await auth.client.is_user_authorized()
        except Exception:
            was_authorized = False
        try:
            await auth.disconnect()
        except Exception:
            pass
        if not was_authorized:
            try:
                auth.cleanup_session()
            except Exception:
                pass
        del user_auths[user_id]

    # Hapus pesan terakhir sebelum mengirim pesan cancel
    await delete_last_message(update, context)

    await update.message.reply_text(
        "❌ Proses login dibatalkan.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def login_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dipanggil oleh ConversationHandler ketika admin idle melebihi
    ``LOGIN_IDLE_TIMEOUT_SECS``. Bersihkan auth dan beri tahu user.

    Catatan: di state TIMEOUT, ``update`` adalah update terakhir yang dipakai
    untuk memulai timer (mungkin dari command awal). ``update.message`` bisa
    saja sudah tidak relevan, jadi kita pakai ``context.bot.send_message`` ke
    ``effective_chat.id`` untuk pengiriman pesan yang aman.
    """
    user_id = update.effective_user.id if update and update.effective_user else None
    chat_id = update.effective_chat.id if update and update.effective_chat else None

    if user_id is not None and user_id in user_auths:
        auth = user_auths[user_id]
        was_authorized = False
        try:
            if auth.client is not None:
                was_authorized = await auth.client.is_user_authorized()
        except Exception:
            was_authorized = False
        try:
            await auth.disconnect()
        except Exception:
            pass
        if not was_authorized:
            try:
                auth.cleanup_session()
            except Exception:
                pass
        del user_auths[user_id]

    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏰ **Proses login dibatalkan**\n\n"
                    f"Tidak ada aktivitas selama {LOGIN_IDLE_TIMEOUT_SECS // 60} menit. "
                    "Silakan kirim /login lagi bila ingin mengulang."
                ),
                parse_mode='Markdown',
            )
        except Exception:
            pass

    # Reset penanda pesan agar tidak menghapus pesan timeout di siklus berikutnya.
    try:
        context.user_data['last_bot_message_id'] = None
    except Exception:
        pass

    return ConversationHandler.END


def get_login_conversation_handler():
    """Mengembalikan ConversationHandler untuk login flow.

    Memakai ``conversation_timeout`` agar alur login otomatis dibatalkan jika
    admin tidak mengirim input apa pun (nomor / OTP / password) selama
    ``LOGIN_IDLE_TIMEOUT_SECS`` detik.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_otp),
                CommandHandler("resend", resend_otp)
            ],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, login_timeout),
                CommandHandler(["cancel", "resend", "login"], login_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("resend", resend_otp)],
        conversation_timeout=LOGIN_IDLE_TIMEOUT_SECS,
    )

