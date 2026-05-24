"""Main bot file - Entry point untuk Telegram Bot"""
# Suppress warning SEBELUM import handlers agar tidak muncul saat modul dimuat
from typing import List, Optional

import logging
import warnings
from telegram.warnings import PTBUserWarning

# Logging minimal: tampilkan hanya WARNING+ supaya konsol bersih.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("telethon").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)
warnings.filterwarnings(
    "ignore",
    message=r".*CallbackQueryHandler.*per_message.*",
    category=PTBUserWarning
)

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, TypeHandler
from telegram.request import HTTPXRequest
from config import (
    ADMIN_USER_IDS,
    BOT_TOKEN,
    get_service_last_valid_date,
    is_service_expired,
)
from handlers.login import start, get_login_conversation_handler, cleanup_login_sessions
from handlers.manage import get_manage_handlers
from handlers.automation import get_automation_handlers, cleanup_auto_monitors
from handlers.service_expiry import service_expiry_gate

# Satu sumber kebenaran untuk menu (/) dan teks notifikasi startup
BOT_MENU_COMMANDS: List[BotCommand] = [
    BotCommand("start", "Panduan & menu utama"),
    BotCommand("login", "Login / buat session"),
    BotCommand("manage", "Kelola session, kontak & undangan"),
    BotCommand("cancel", "Batalkan proses yang sedang berjalan"),
    BotCommand("resend", "Kirim ulang kode OTP (saat login)"),
]


def build_startup_notify_text(bot_username: Optional[str]) -> str:
    """Teks notifikasi saat bot start (Tanpa parse_mode, aman dari karakter khusus)."""
    last = get_service_last_valid_date().strftime("%Y-%m-%d")
    lines_commands = [f"/{c.command} — {c.description}" for c in BOT_MENU_COMMANDS]
    commands_block = "\n".join(lines_commands)

    who = f"@{bot_username}" if bot_username else "Bot"
    header = f"🤖 {who} telah dijalankan\n"

    if is_service_expired():
        status = (
            f"⏳ Masa layanan: KADALUARSA (batas konfigurasi: {last}).\n"
            "Menu perintah (/) dikosongkan untuk pengguna. Perbarui tanggal di config.py lalu restart.\n\n"
        )
    else:
        status = (
            f"📅 Masa layanan: aktif sampai {last} (tanggal tersebut masih dihitung aktif).\n\n"
        )

    return (
        f"{header}"
        f"{status}"
        f"📋 Perintah bot:\n{commands_block}"
    )


async def register_bot_commands(application: Application) -> None:
    """Mendaftarkan perintah ke menu Telegram (/) saat bot pertama jalankan."""
    try:
        if is_service_expired():
            await application.bot.set_my_commands([])
            print("⏳ Layanan kadaluarsa: menu perintah (/) dikosongkan.")
        else:
            await application.bot.set_my_commands(BOT_MENU_COMMANDS)
            listed = ", ".join(f"/{c.command}" for c in BOT_MENU_COMMANDS)
            print(f"✅ Menu perintah Telegram terdaftar: {listed}")
    except Exception as e:
        print(f"⚠️ Gagal mendaftarkan menu perintah (/): {e}")


async def notify_admins_startup(application: Application) -> None:
    """Kirim DM ke setiap admin di ADMIN_USER_IDS tentang status layanan & daftar perintah."""
    if not ADMIN_USER_IDS:
        print("ℹ️ Startup notifikasi Telegram dilewati (ADMIN_USER_IDS kosong).")
        return
    try:
        me = await application.bot.get_me()
        uname = (me.username or "").strip() or None
    except Exception as e:
        uname = None
        print(f"⚠️ Gagal ambil info bot untuk notifikasi: {e}")

    text = build_startup_notify_text(uname)
    for admin_id in ADMIN_USER_IDS:
        try:
            await application.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            print(f"⚠️ Gagal kirim startup Telegram ke admin {admin_id}: {e}")


async def on_post_init(application: Application) -> None:
    await register_bot_commands(application)
    await notify_admins_startup(application)


def main():
    """Fungsi utama untuk menjalankan bot"""
    if not BOT_TOKEN:
        print("❌ Error: BOT_TOKEN tidak ditemukan!")
        print("Silakan buat file .env dan isi BOT_TOKEN")
        return
    
    try:
        # ─── HTTPX request: lebih tahan terhadap koneksi server-drop ──────────
        # `httpx.RemoteProtocolError: Server disconnected without sending a
        # response` adalah error transient bila pool koneksi terlalu kecil
        # atau koneksi idle di-drop server Telegram. Setting di bawah ini
        # memperbesar pool dan timeout supaya bot tetap responsif.
        #
        # - connection_pool_size: jumlah koneksi keep-alive ke api.telegram.org.
        #   Default 1 tidak cukup untuk concurrent_updates(True). 64 cukup
        #   longgar untuk bot ini.
        # - pool_timeout: berapa lama menunggu slot koneksi di pool sebelum
        #   error. Naikkan supaya request paralel tidak gagal duluan.
        # - connect/read/write_timeout: dilonggarkan agar tidak salah-tafsir
        #   slow-network sebagai disconnect.
        request = HTTPXRequest(
            connection_pool_size=64,
            pool_timeout=20.0,
            connect_timeout=20.0,
            read_timeout=40.0,
            write_timeout=40.0,
        )
        get_updates_request = HTTPXRequest(
            connection_pool_size=8,
            pool_timeout=20.0,
            connect_timeout=20.0,
            # Long-poll: read_timeout HARUS lebih besar dari polling timeout PTB.
            # Default PTB long-poll = 10 detik; kita beri buffer.
            read_timeout=60.0,
            write_timeout=40.0,
        )

        # Buat Application dengan builder pattern.
        # ``concurrent_updates(True)`` (PTB v20+) memproses update dari
        # *user/conversation berbeda* secara paralel. Update dari user yang sama
        # tetap diserialisasi oleh ConversationHandler (default ``block=True``),
        # jadi state percakapan tetap konsisten. Tanpa ini, operasi panjang
        # (sweep validitas sessions, scrape multi-session, dll.) memblokir
        # semua perintah lain dan bot tampak "tidak respon" saat banyak sesi.
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(on_post_init)
            .concurrent_updates(True)
            .request(request)
            .get_updates_request(get_updates_request)
            .build()
        )

        if is_service_expired():
            last_ok = get_service_last_valid_date().strftime("%Y-%m-%d")
            print(
                f"⏳ PERINGATAN: Masa layanan bot sudah lewat (akhir konfigurasi: {last_ok}). "
                "Perbarui tanggal di config.py lalu restart."
            )
        else:
            until = get_service_last_valid_date().strftime("%Y-%m-%d")
            print(f"📅 Masa layanan aktif sampai: {until} (termasuk tanggal tersebut)")

        # ─── Masa aktif: 1 handler di grup 0 (blocking). Semua fitur di grup 1. ───
        # Di PTB v20+: per grup hanya 1 handler blocking yang "menang"; non-blocking + ALL
        # sering membuat /start tidak pernah tercapai. Pisah grup menjamin urutan ini.
        application.add_handler(TypeHandler(Update, service_expiry_gate), group=0)

        application.add_handler(CommandHandler("start", start), group=1)
        application.add_handler(get_login_conversation_handler(), group=1)
        for handler in get_automation_handlers():
            application.add_handler(handler, group=1)
        for handler in get_manage_handlers():
            application.add_handler(handler, group=1)

        # ─── Error handler global: serap NetworkError transient ──────────────
        # ``httpx.RemoteProtocolError``, ``ReadError``, dan kerabatnya muncul
        # ketika koneksi ke api.telegram.org di-drop oleh server (idle, NAT,
        # dll.). PTB polling akan melanjutkan sendiri di siklus berikutnya;
        # error itu hanya bising di konsol. Kita ringkas jadi satu baris
        # peringatan agar log tetap berguna untuk error nyata.
        from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest
        import traceback as _traceback

        async def _global_error_handler(update, context):  # type: ignore[no-redef]
            err = context.error
            if isinstance(err, (NetworkError, TimedOut)):
                # Transient — biarkan PTB retry di getUpdates berikutnya.
                logging.getLogger("ptb").warning(
                    "Transient network error: %s. Bot akan melanjutkan polling.",
                    str(err)[:160],
                )
                return
            if isinstance(err, RetryAfter):
                logging.getLogger("ptb").warning(
                    "Telegram rate-limit RetryAfter: tunggu %s detik.",
                    getattr(err, "retry_after", "?"),
                )
                return
            if isinstance(err, BadRequest):
                logging.getLogger("ptb").warning("BadRequest: %s", err)
                return
            # Untuk error lain, tampilkan traceback agar bisa diinvestigasi.
            logging.getLogger("ptb").error(
                "Unhandled error in handler: %s\n%s",
                err,
                "".join(_traceback.format_exception(type(err), err, err.__traceback__)),
            )

        application.add_error_handler(_global_error_handler)
        
        # Tambahkan shutdown callback untuk membersihkan SEMUA koneksi aktif
        async def post_shutdown(app):
            """Callback yang dipanggil setelah bot berhenti.
            Membersihkan:
            1. Auto view monitor tasks
            2. Active view boosting tasks  
            3. Login sessions yang masih terbuka
            """
            print("\n🔄 Membersihkan semua koneksi aktif...")
            await cleanup_auto_monitors()
            await cleanup_login_sessions()
            print("✅ Semua koneksi dibersihkan. Bot berhenti.")
        
        application.post_shutdown = post_shutdown
        
        print("🤖 Bot sedang berjalan...")
        print("Tekan Ctrl+C untuk menghentikan bot")
        
        # Jalankan bot
        application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        
    except Exception as e:
        print(f"❌ Error saat menjalankan bot: {str(e)}")
        print("\n💡 Tips:")
        print("1. Pastikan Python versi 3.8-3.11 (python-telegram-bot tidak support Python 3.14)")
        print("2. Update library: pip install --upgrade python-telegram-bot")
        print("3. Atau install ulang: pip install -r requirements.txt --upgrade")
        raise


if __name__ == "__main__":
    main()
