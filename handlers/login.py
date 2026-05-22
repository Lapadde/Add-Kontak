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


# ─────────────────────────────────────────────────────────────────────────────
# Konstanta teks UI (Markdown v1). Pemakaian backslash untuk escape karakter
# `.` / `!` tidak diperlukan di v1 — hanya `_`, `*`, `[`, `` ` `` yang istimewa.
# Gunakan tanda kutip ` untuk inline code; itu cara paling aman menampilkan
# nomor telepon / contoh perintah.
# ─────────────────────────────────────────────────────────────────────────────

ACCESS_DENIED_TEXT = (
    "🚫 *Akses Ditolak*\n\n"
    "Akun Telegram Anda tidak terdaftar sebagai admin bot ini, "
    "jadi semua perintah diabaikan.\n\n"
    "Hubungi administrator agar `user_id` Anda dimasukkan ke daftar admin "
    "(`ADMIN_USER_IDS` di `config.py`)."
)


def _login_success_text(phone_number: str, me) -> str:
    """Format pesan sukses login yang konsisten di 3 cabang (phone/OTP/password)."""
    first_name = escape_markdown(me.first_name or "(tanpa nama)", version=1)
    nomor = escape_markdown(phone_number, version=1)
    path = escape_markdown(_format_session_saved_path(phone_number), version=1)
    return (
        "✅ *Login berhasil!*\n\n"
        f"👤 Nama akun : {first_name}\n"
        f"📱 Nomor      : {nomor}\n"
        f"🆔 User ID    : `{me.id}`\n\n"
        f"📂 Session disimpan di:\n`{path}`\n\n"
        "Anda dapat memakai session ini di menu /manage. "
        "Untuk login akun lain, kirim /login lagi."
    )


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
            ACCESS_DENIED_TEXT,
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    welcome_msg = (
        "🤖 *Bot Userbot Telegram*\n\n"
        "Bot ini membantu Anda *login* ke beberapa akun Telegram (membuat "
        "session Telethon) lalu *mengelola* akun-akun tsb untuk scrape grup, "
        "mengundang member, dll.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔑 *Alur login (3 langkah singkat)*\n"
        "*1.* Kirim nomor telepon akun yang ingin di-login\n"
        "    Format internasional: `+628123456789` (ID), `+14155552671` (US), "
        "`+447911123456` (UK), dst.\n"
        "    Khusus Indonesia, `08123456789` juga diterima.\n"
        "*2.* Telegram akan mengirimkan kode OTP 5 digit ke akun Anda. "
        "Kirimkan kode itu ke bot.\n"
        "*3.* Bila akun Anda mengaktifkan *Two-Step Verification (2FA)*, "
        "bot akan meminta password 2FA Anda.\n\n"
        "⏰ *Catatan:* jika tidak ada balasan selama 3 menit di langkah mana "
        "pun, proses login akan dibatalkan otomatis.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 *Daftar perintah*\n"
        "/login — mulai proses login akun baru\n"
        "/manage — kelola session, scrape grup, undang member\n"
        "/resend — kirim ulang kode OTP (hanya saat menunggu OTP)\n"
        "/cancel — batalkan proses login yang sedang berjalan\n\n"
        "Ketik /login sekarang untuk memulai."
    )
    await update.message.reply_text(welcome_msg, parse_mode='Markdown')
    return ConversationHandler.END


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai proses login"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            ACCESS_DENIED_TEXT,
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Cek apakah user sudah punya proses login aktif
    if user_id in user_auths:
        await update.message.reply_text(
            "⚠️ *Masih ada proses login yang berjalan*\n\n"
            "Selesaikan dulu langkahnya, atau ketik /cancel untuk "
            "membatalkan proses login sebelumnya. Setelah itu kirim /login lagi.",
            parse_mode='Markdown',
        )
        return ConversationHandler.END

    # Hapus pesan user agar nomor/teks sensitif tidak menumpuk di chat
    try:
        await update.message.delete()
    except Exception:
        pass

    await send_and_save_message(
        update, context,
        "📱 *Langkah 1/3 — Nomor Telepon*\n\n"
        "Kirim nomor akun Telegram yang ingin di-login dalam format "
        "internasional (`+<kode_negara><nomor>`).\n\n"
        "*Contoh:*\n"
        "• `+628123456789` — Indonesia\n"
        "• `+14155552671` — Amerika Serikat\n"
        "• `+447911123456` — Inggris\n"
        "• `+491701234567` — Jerman\n\n"
        "💡 *Tips:*\n"
        "• Khusus Indonesia, format lokal `08123456789` juga diterima.\n"
        "• Pastikan akun yang nomornya Anda kirim sedang bisa Anda akses, "
        "karena kode OTP dikirim ke akun tersebut.\n"
        "• Ketik /cancel kapan saja untuk membatalkan.\n"
        "• Jika diam selama 3 menit, proses login dibatalkan otomatis.",
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
            ACCESS_DENIED_TEXT,
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
            "❌ *Format nomor tidak valid*\n\n"
            "Bot tidak menemukan cukup digit pada teks yang Anda kirim.\n\n"
            "Kirim ulang nomor dalam format internasional, misal:\n"
            "• `+628123456789` (Indonesia)\n"
            "• `+14155552671` (US)\n"
            "• `+447911123456` (UK)\n\n"
            "Khusus Indonesia, `08123456789` juga diterima.\n\n"
            "Atau ketik /cancel untuk membatalkan.",
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
            "❌ *Nomor masih belum sesuai standar internasional*\n\n"
            "Format yang diharapkan: `+<kode_negara><nomor>` "
            "dengan total 8–15 digit setelah tanda `+`.\n\n"
            "*Contoh benar:*\n"
            "• `+628123456789` (Indonesia)\n"
            "• `+14155552671` (US)\n"
            "• `+447911123456` (UK)\n\n"
            "Kirim ulang nomornya, atau ketik /cancel untuk membatalkan.",
            parse_mode='Markdown'
        )
        return PHONE

    try:
        await send_and_save_message(
            update, context,
            "⏳ *Memproses nomor…*\n\n"
            f"Menghubungkan ke server Telegram untuk akun `{escape_markdown(phone_number, version=1)}`.\n"
            "Mohon tunggu beberapa detik.",
            parse_mode='Markdown',
        )

        # Buat TelethonAuth instance
        auth = TelethonAuth(phone_number)
        user_auths[user_id] = auth

        # `connect()` dipanggil sekali. Telethon di dalam `connect()`:
        # - kalau sudah authorized → return True.
        # - kalau belum authorized → otomatis `send_code_request` lalu return False.
        connected = await auth.connect()
        if connected:
            # Akun sudah authorized berkat file session lama yang masih valid.
            try:
                me = await auth.get_me()
                await send_and_save_message(
                    update, context,
                    _login_success_text(phone_number, me)
                    + "\n\nℹ️ Tidak perlu OTP karena session lama untuk nomor "
                      "ini masih valid.",
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
        intro_lines = [
            "📨 *Kode OTP terkirim*\n",
            f"Telegram baru saja mengirim kode 5 digit ke akun "
            f"`{escape_markdown(phone_number, version=1)}` "
            "(via app Telegram, bukan SMS).\n",
        ]
        if had_stale_session:
            intro_lines.append(
                "ℹ️ Session lama untuk nomor ini ditemukan tapi sudah tidak "
                "valid lagi. Diperlukan login ulang dengan OTP.\n"
            )
        body_lines = [
            "🔢 *Langkah 2/3 — Kode OTP*\n",
            "Kirim kode OTP yang Anda terima ke chat ini.",
            "Format: 5 digit angka, contoh `12345`.\n",
            "💡 *Tips:*",
            "• Buka aplikasi Telegram resmi → cek pesan masuk dari akun "
            "`Telegram` atau notifikasi *Login Code*.",
            "• Kode OTP berlaku ~5 menit. Bila kedaluwarsa, ketik /resend.",
            "• Bila tidak menerima kode dalam 1–2 menit, ketik /resend.",
            "• Ketik /cancel untuk membatalkan.",
            "• Diam selama 3 menit → login dibatalkan otomatis.",
        ]
        await send_and_save_message(
            update, context,
            "\n".join(intro_lines + body_lines),
            parse_mode='Markdown'
        )
        return OTP

    except Exception as e:
        error_msg = str(e)
        low = error_msg.lower()
        if "all available options" in low or "resendcode" in low:
            friendly = (
                "⚠️ *Semua metode pengiriman OTP sudah dipakai*\n\n"
                "Telegram membatasi pengiriman kode untuk nomor ini sementara waktu. "
                "Tunggu beberapa menit, lalu ketik /login lagi untuk minta kode baru.\n\n"
                "Atau ketik /cancel untuk membatalkan."
            )
        elif "flood" in low or "wait of" in low:
            friendly = (
                "⏳ *Terkena FloodWait dari Telegram*\n\n"
                f"Pesan asli: `{escape_markdown(error_msg, version=1)}`\n\n"
                "Tunggu durasi yang disebutkan di pesan asli lalu ketik /login lagi. "
                "Atau ketik /cancel untuk membatalkan."
            )
        elif "phone_number" in low and ("invalid" in low or "banned" in low):
            friendly = (
                "❌ *Nomor ditolak Telegram*\n\n"
                f"Pesan asli: `{escape_markdown(error_msg, version=1)}`\n\n"
                "Pastikan nomor yang Anda kirim benar-benar terdaftar di Telegram "
                "dan tidak diblokir. Ketik /login untuk coba nomor lain, atau "
                "/cancel untuk membatalkan."
            )
        else:
            friendly = (
                "❌ *Gagal memproses nomor*\n\n"
                f"Pesan asli: `{escape_markdown(error_msg, version=1)}`\n\n"
                "Ketik /login untuk mencoba lagi, atau /cancel untuk membatalkan."
            )
        await send_and_save_message(update, context, friendly, parse_mode='Markdown')
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
            ACCESS_DENIED_TEXT,
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
            "⚠️ *Sesi login tidak ditemukan*\n\n"
            "Mungkin Anda belum memulai /login, atau prosesnya sudah dibatalkan "
            "(karena /cancel, error, atau timeout 3 menit).\n\n"
            "Ketik /login untuk memulai dari awal.",
            parse_mode='Markdown',
        )
        return ConversationHandler.END

    auth = user_auths[user_id]

    # Validasi OTP harus angka.
    if not otp_code.isdigit():
        await send_and_save_message(
            update, context,
            "❌ *Kode OTP harus angka saja*\n\n"
            "Format: 5 digit angka, contoh `12345`.\n"
            "Hapus spasi/strip/huruf, lalu kirim ulang.\n\n"
            "Ketik /resend untuk minta kode baru, atau /cancel untuk membatalkan.",
            parse_mode='Markdown',
        )
        return OTP

    try:
        await send_and_save_message(
            update, context,
            "⏳ *Memverifikasi kode OTP…*\n\n"
            "Mengirim kode ke server Telegram. Mohon tunggu sebentar.",
            parse_mode='Markdown',
        )

        success, message = await auth.sign_in(otp_code=otp_code)

        if success:
            try:
                me = await auth.get_me()
                await send_and_save_message(
                    update, context,
                    _login_success_text(auth.phone_number, me),
                    parse_mode='Markdown'
                )
            finally:
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
            context.user_data['last_bot_message_id'] = None
            return ConversationHandler.END
        else:
            if "Password diperlukan" in message or "password" in message.lower():
                await send_and_save_message(
                    update, context,
                    "🔐 *Langkah 3/3 — Password 2FA (Two-Step Verification)*\n\n"
                    "Akun ini mengaktifkan *Two-Step Verification*. Kirim "
                    "*Cloud Password* Anda — yaitu password yang Anda set di "
                    "Telegram (Settings → Privacy and Security → Two-Step "
                    "Verification), *bukan* password media sosial atau email.\n\n"
                    "💡 *Tips:*\n"
                    "• Kalau lupa password 2FA, Anda bisa mereset lewat email "
                    "pemulihan di app Telegram resmi.\n"
                    "• Bot akan menghapus pesan password Anda dari chat untuk "
                    "alasan keamanan.\n"
                    "• Ketik /cancel untuk membatalkan.\n"
                    "• Diam selama 3 menit → login dibatalkan otomatis.",
                    parse_mode='Markdown'
                )
                return PASSWORD
            elif message == "RESEND_NEEDED":
                await send_and_save_message(
                    update, context,
                    "⚠️ *Semua metode verifikasi OTP sudah dipakai*\n\n"
                    "Ketik /resend untuk meminta kode OTP baru, atau /cancel "
                    "untuk membatalkan.",
                    parse_mode='Markdown'
                )
                return OTP
            else:
                low = (message or "").lower()
                if "expired" in low or "phonecodeexpired" in low:
                    detail = (
                        "Kode OTP sudah kedaluwarsa.\n"
                        "Ketik /resend untuk minta kode baru."
                    )
                elif "invalid" in low or "phonecodeinvalid" in low:
                    detail = (
                        "Kode OTP yang Anda kirim tidak cocok.\n"
                        "Periksa lagi 5 digit yang Telegram kirim, lalu kirim ulang.\n"
                        "Jika sudah hilang, ketik /resend."
                    )
                else:
                    detail = (
                        f"Pesan dari Telegram: `{escape_markdown(message, version=1)}`\n"
                        "Coba kirim ulang OTP, atau /resend untuk kode baru."
                    )
                await send_and_save_message(
                    update, context,
                    "❌ *Verifikasi OTP gagal*\n\n"
                    f"{detail}\n\n"
                    "Ketik /cancel untuk membatalkan.",
                    parse_mode='Markdown'
                )
                return OTP

    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "flood" in low or "wait of" in low:
            friendly = (
                "⏳ *FloodWait dari Telegram saat verifikasi OTP*\n\n"
                f"Pesan asli: `{escape_markdown(msg, version=1)}`\n\n"
                "Tunggu durasi tersebut sebelum mencoba lagi. Bisa juga ketik "
                "/cancel untuk membatalkan sekarang."
            )
        else:
            friendly = (
                "❌ *Gagal memverifikasi OTP*\n\n"
                f"Pesan asli: `{escape_markdown(msg, version=1)}`\n\n"
                "Kirim ulang kode, atau /resend untuk minta kode baru, atau "
                "/cancel untuk membatalkan."
            )
        await send_and_save_message(update, context, friendly, parse_mode='Markdown')
        return OTP


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima password 2FA dan menyelesaikan login"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            ACCESS_DENIED_TEXT,
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
            "⚠️ *Sesi login tidak ditemukan*\n\n"
            "Mungkin proses login sudah dibatalkan (karena /cancel, error, "
            "atau timeout 3 menit).\n\n"
            "Ketik /login untuk memulai dari awal.",
            parse_mode='Markdown',
        )
        return ConversationHandler.END

    auth = user_auths[user_id]

    try:
        await send_and_save_message(
            update, context,
            "⏳ *Memverifikasi password 2FA…*\n\n"
            "Mengirim password ke server Telegram. Mohon tunggu sebentar.",
            parse_mode='Markdown',
        )

        success, message = await auth.sign_in(password=password)

        if success:
            try:
                me = await auth.get_me()
                await send_and_save_message(
                    update, context,
                    _login_success_text(auth.phone_number, me),
                    parse_mode='Markdown'
                )
            finally:
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
            context.user_data['last_bot_message_id'] = None
            return ConversationHandler.END
        else:
            await send_and_save_message(
                update, context,
                "❌ *Password 2FA salah*\n\n"
                f"Pesan dari Telegram: `{escape_markdown(message, version=1)}`\n\n"
                "Kirim ulang Cloud Password Anda (pastikan huruf besar/kecil "
                "dan karakter khusus benar).\n"
                "Ketik /cancel untuk membatalkan.",
                parse_mode='Markdown'
            )
            return PASSWORD

    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "flood" in low or "wait of" in low:
            friendly = (
                "⏳ *FloodWait dari Telegram saat verifikasi password*\n\n"
                f"Pesan asli: `{escape_markdown(msg, version=1)}`\n\n"
                "Tunggu durasi tersebut sebelum mencoba lagi, atau ketik "
                "/cancel untuk membatalkan."
            )
        else:
            friendly = (
                "❌ *Gagal memverifikasi password 2FA*\n\n"
                f"Pesan asli: `{escape_markdown(msg, version=1)}`\n\n"
                "Kirim ulang password, atau /cancel untuk membatalkan."
            )
        await send_and_save_message(update, context, friendly, parse_mode='Markdown')
        return PASSWORD


async def resend_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengirim ulang kode OTP"""
    user_id = update.effective_user.id

    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            ACCESS_DENIED_TEXT,
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    if user_id not in user_auths:
        await update.message.reply_text(
            "⚠️ *Sesi login tidak ditemukan*\n\n"
            "/resend hanya bisa dipakai saat bot sedang menunggu OTP dari Anda.\n"
            "Ketik /login untuk memulai proses login.",
            parse_mode='Markdown',
        )
        return ConversationHandler.END

    auth = user_auths[user_id]

    try:
        await send_and_save_message(
            update, context,
            "⏳ *Mengirim ulang kode OTP…*\n\n"
            "Meminta Telegram mengirimkan kode baru. Mohon tunggu sebentar.",
            parse_mode='Markdown',
        )

        success, message = await auth.resend_code()

        if success:
            await send_and_save_message(
                update, context,
                "✅ *Kode OTP baru terkirim*\n\n"
                f"Cek app Telegram Anda di akun `{escape_markdown(auth.phone_number, version=1)}` "
                "untuk pesan dari `Telegram` / notifikasi *Login Code*.\n\n"
                "Kirim 5 digit angka itu ke chat ini. Format: `12345`.\n"
                "Ketik /cancel untuk membatalkan.",
                parse_mode='Markdown'
            )
            return OTP
        else:
            await send_and_save_message(
                update, context,
                "❌ *Gagal mengirim ulang OTP*\n\n"
                f"Pesan dari Telegram: `{escape_markdown(message or '(tanpa detail)', version=1)}`\n\n"
                "Tunggu beberapa saat lalu coba /resend lagi, atau ketik "
                "/cancel untuk membatalkan.",
                parse_mode='Markdown',
            )
            return OTP

    except Exception as e:
        await send_and_save_message(
            update, context,
            "❌ *Error saat mengirim ulang OTP*\n\n"
            f"Pesan asli: `{escape_markdown(str(e), version=1)}`\n\n"
            "Coba /resend lagi, atau /cancel untuk membatalkan.",
            parse_mode='Markdown',
        )
        return OTP


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan proses login"""
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await update.message.reply_text(
            ACCESS_DENIED_TEXT,
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
        "🛑 *Proses login dibatalkan*\n\n"
        "Anda dapat memulai lagi kapan saja dengan /login.",
        parse_mode='Markdown',
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
                    "⏰ *Proses login dibatalkan otomatis*\n\n"
                    f"Tidak ada balasan apa pun selama "
                    f"{LOGIN_IDLE_TIMEOUT_SECS // 60} menit.\n\n"
                    "Ketik /login untuk memulai lagi dari awal."
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

