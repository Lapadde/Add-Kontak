"""Gerbang masa aktif layanan: setelah tanggal di config, blokir fitur dan arahkan ke kontak dukungan."""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ApplicationHandlerStop
from telegram.helpers import escape_markdown

from config import (
    get_service_last_valid_date,
    is_service_expired,
    support_telegram_url,
    support_whatsapp_url,
    SUPPORT_TELEGRAM_USERNAME,
    SUPPORT_WHATSAPP_NUMBER,
)


def build_expiry_reply_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📱 WhatsApp", url=support_whatsapp_url()
                ),
                InlineKeyboardButton(
                    "✈️ Telegram", url=support_telegram_url()
                ),
            ],
        ]
    )


def build_expiry_message_text() -> str:
    last = get_service_last_valid_date()
    last_s = escape_markdown(last.strftime("%d/%m/%Y"), version=1)
    wa_esc = escape_markdown(SUPPORT_WHATSAPP_NUMBER, version=1)
    tg_esc = escape_markdown("@" + SUPPORT_TELEGRAM_USERNAME.lstrip("@"), version=1)
    return (
        "⏳ **Masa layanan bot telah berakhir**\n\n"
        "Silakan **perpanjang** layanan melalui:\n"
        f"• WhatsApp: `{wa_esc}`\n"
        f"• Telegram: {tg_esc}\n\n"
        "Gunakan tombol di bawah untuk membuka chat\\. "
        "Setelah tanggal di config\\.py diperbarui, **restart bot** agar fitur aktif kembali\\.\n\n"
        f"📅 Akhir masa aktif terkonfigurasi: **{last_s}**"
    )


async def _reply_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = build_expiry_message_text()
    keyboard = build_expiry_reply_markup()
    parse_mode = "Markdown"
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)
        return
    if update.edited_message:
        await update.edited_message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)
        return
    q = update.callback_query
    if q:
        await q.answer("Layanan berakhir. Hubungi admin untuk perpanjang.")
        try:
            await q.edit_message_text(text, reply_markup=keyboard, parse_mode=parse_mode)
        except Exception:
            await q.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)


async def service_expiry_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Satu gerbang untuk semua jenis update. Grup handler harus 0; handler lain di grup 1+.

    PTB hanya memproses maks. satu handler blocking per grup. Tanpa expired: return saja,
    lalu grup berikutnya (/start, /login, …) dijalankan. Saat expired: balas + Stop total.
    """
    if not is_service_expired():
        return
    await _reply_expired(update, context)
    raise ApplicationHandlerStop
