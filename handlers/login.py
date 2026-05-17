"""Handlers untuk login flow"""
import re
import asyncio
from telegram import Update, ReplyKeyboardRemove
from telegram.helpers import escape_markdown
from telegram.ext import ConversationHandler, CommandHandler, MessageHandler, filters, ContextTypes
from telethon_client import TelethonAuth
from utils.helpers import validate_phone, is_admin
from config import SESSION_DIR

# State untuk ConversationHandler
PHONE, OTP, PASSWORD = range(3)

# Dictionary untuk menyimpan auth objects per user (shared state)
# Key: user_id (Telegram user ID), Value: TelethonAuth instance
user_auths = {}

# Timeout untuk cleanup auth objects yang tidak aktif (dalam detik)
AUTH_TIMEOUT = 300  # 5 menit


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
        "1. Kirim nomor telepon Anda (format: +628123456789 atau 08123456789)\n"
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
        "Silakan kirim nomor telepon Anda.\n"
        "Format: +628123456789 atau 08123456789\n\n"
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
    
    # Validasi nomor telepon
    if not validate_phone(phone_number):
        await send_and_save_message(
            update, context,
            "❌ Format nomor telepon tidak valid!\n\n"
            "Silakan kirim nomor telepon yang valid.\n"
            "Contoh: +628123456789 atau 08123456789"
        )
        return PHONE
    
    # Normalisasi nomor telepon
    phone_number = re.sub(r'\D', '', phone_number)
    if not phone_number.startswith('+'):
        phone_number = '+' + phone_number
    
    try:
        await send_and_save_message(
            update, context,
            "⏳ Memproses nomor telepon...\n"
            "Mohon tunggu sebentar..."
        )
        
        # Buat TelethonAuth instance
        auth = TelethonAuth(phone_number)
        user_auths[user_id] = auth
        
        # Cek apakah session sudah ada
        if auth.is_session_exists():
            await send_and_save_message(
                update, context,
                "✅ Session sudah ada untuk nomor ini!\n"
                "Mencoba menghubungkan..."
            )
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
                        f"Session tersimpan di: `{escape_markdown(f'sessions/users/{phone_number}.session', version=1)}`",
                        parse_mode='Markdown'
                    )
                finally:
                    # Pastikan disconnect
                    try:
                        await auth.disconnect()
                    except Exception:
                        pass
                if user_id in user_auths:
                    del user_auths[user_id]
                # Reset last_bot_message_id agar pesan login berhasil tidak dihapus
                context.user_data['last_bot_message_id'] = None
                return ConversationHandler.END
        
        # Request OTP
        await auth.connect()
        await send_and_save_message(
            update, context,
            "✅ Kode OTP telah dikirim ke Telegram Anda!\n\n"
            "📱 **Langkah 2: Kode OTP**\n\n"
            "Silakan kirim kode OTP yang Anda terima.\n"
            "Format: 12345 (5 digit angka)\n\n"
            "💡 **Tips:**\n"
            "- Kode OTP berlaku selama 5 menit\n"
            "- Jika tidak menerima kode, gunakan /resend\n"
            "- Gunakan /cancel untuk membatalkan",
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
        if user_id in user_auths:
            auth = user_auths[user_id]
            try:
                await auth.disconnect()
            except Exception:
                pass  # Ignore error saat disconnect
            if not auth.is_connected:
                auth.cleanup_session()
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
                    f"Session tersimpan di: `{escape_markdown(f'sessions/users/{auth.phone_number}.session', version=1)}`",
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
        # Cleanup jika error
        if user_id in user_auths:
            auth = user_auths[user_id]
            try:
                if not auth.is_connected:
                    auth.cleanup_session()
                await auth.disconnect()
            except Exception:
                pass
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
                    f"Session tersimpan di: `{escape_markdown(f'sessions/users/{auth.phone_number}.session', version=1)}`",
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
        # Cleanup jika error
        if user_id in user_auths:
            auth = user_auths[user_id]
            try:
                if not auth.is_connected:
                    auth.cleanup_session()
                await auth.disconnect()
            except Exception:
                pass
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
        return
    
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
        await auth.disconnect()
        if not auth.is_connected:
            auth.cleanup_session()
        del user_auths[user_id]
    
    # Hapus pesan terakhir sebelum mengirim pesan cancel
    await delete_last_message(update, context)
    
    await update.message.reply_text(
        "❌ Proses login dibatalkan.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


def get_login_conversation_handler():
    """Mengembalikan ConversationHandler untuk login flow"""
    return ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_otp),
                CommandHandler("resend", resend_otp)
            ],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("resend", resend_otp)],
    )

