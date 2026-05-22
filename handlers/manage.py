"""Handlers untuk manajemen sessions"""
import os
import re
import asyncio
import logging
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telethon_client import TelethonAuth, FLOOD_WAIT_ABORT_ABOVE_SEC
from config import SESSION_DIR
from utils.helpers import is_admin
from utils.invite_flood_state import (
    clear_invite_flood,
    get_invite_flood_status,
    human_duration_seconds,
    record_invite_flood,
)
from utils.session_order import (
    forget_session as _forget_session_order,
    get_ordered_session_phones,
)
from handlers.automation import automation_menu

logger = logging.getLogger(__name__)

# State ConversationHandler: Invite kontak ke grup
INVITE_CONTACTS_ASK_LINK = 60

# Gabung grup (session dipilih) dari link / username
JOIN_SESSION_GROUP_ASK_LINK = 62

# Manage menu: gabung satu grup untuk SEMUA file session
MANAGE_JOIN_ALL_GROUP_ASK_LINK = 63

# Manage: scrape sumber → tujuan, multi-session, undangan paralel, FloodWait per-akun
MANAGE_SCRAPE_ALL_RANGE = 83
MANAGE_SCRAPE_ALL_PER_SESSION_CAP = 87
MANAGE_SCRAPE_ALL_SOURCE = 84
MANAGE_SCRAPE_ALL_TARGET = 85
MANAGE_SCRAPE_ALL_FILTER = 86
MANAGE_SCRAPE_INVITES_PER_ROUND = 3
# Batas maksimum cap per session yang valid (anti FloodWait jangka panjang).
MANAGE_SCRAPE_PER_SESSION_CAP_MAX = 50
# Edit pesan progres tiap N gelombang agar tidak spam (selain milestone penting).
SCRAPE_ALL_PROGRESS_EVERY_N_WAVES = 3

# State ConversationHandler: Scrape anggota grup sumber → pilih grup (inline) → undang ke tujuan
SCRAPE_GROUP_PICK_SOURCE = 70
SCRAPE_GROUP_PICK_TARGET = 71
SCRAPE_PAGE_SIZE = 10


async def invite_contacts_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur invite kontak dari tombol inline."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.edit_message_text(
            "❌ **Akses Ditolak**",
            parse_mode='Markdown',
        )
        return ConversationHandler.END
    data = query.data or ""
    prefix = "invite_contacts:"
    if not data.startswith(prefix):
        return ConversationHandler.END
    phone = data[len(prefix):]
    if not phone:
        await query.edit_message_text("❌ Session tidak valid.")
        return ConversationHandler.END
    context.user_data['invite_contacts_phone'] = phone
    phone_esc = escape_markdown(phone, version=1)
    await query.edit_message_text(
        f"👥 **Invite Kontak ke Grup**\n\n"
        f"📱 Session: `{phone_esc}`\n\n"
        f"Kirim **link undangan grup** atau **@username** / URL publik, contoh:\n"
        f"• `https://t.me/+xxxxxxxx`\n"
        f"• `https://t.me/namaGrup`\n"
        f"• `@namaGrup`\n\n"
        f"⚠️ Jika session **belum** ada di grup, bot akan **bergabung dulu**; untuk grup tertutup "
        f"kirim **link undangan** \\(`t.me/+…`\\)\\. Undang kontak biasanya butuh **hak admin**\\. "
        f"Telegram membatasi undangan besar \\- proses bisa lama atau sebagian gagal\\.\n\n"
        f"/cancel untuk batal\\.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Batal", callback_data="cancel_invite_contacts")],
        ]),
    )
    return INVITE_CONTACTS_ASK_LINK


async def invite_contacts_receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima link grup dan jalankan undangan kontak via Telethon."""

    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Akses ditolak.")
        return ConversationHandler.END
    phone = context.user_data.get('invite_contacts_phone')
    if not phone:
        await update.message.reply_text("❌ Sesi tidak ditemukan. Ulangi dari menu session.")
        return ConversationHandler.END
    link = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    status_msg = await update.message.reply_text(
        "⏳ Mengundang kontak ke grup\\.\\.\\. mohon tunggu\\.\\.",
        parse_mode='Markdown',
    )
    auth = TelethonAuth(phone)
    result = None
    try:
        if not auth.is_session_exists():
            await status_msg.edit_text("❌ File session tidak ditemukan.")
            return ConversationHandler.END
        connected = await auth.connect()
        if not connected:
            await status_msg.edit_text(
                "❌ Session belum login\\. Gunakan `/login` untuk mengotentikasi ulang\\.",
                parse_mode='Markdown',
            )
            return ConversationHandler.END
        result = await auth.invite_contacts_to_group(link)
    finally:
        try:
            await auth.disconnect()
        except Exception:
            pass
    context.user_data.pop('invite_contacts_phone', None)
    if result is None:
        await status_msg.edit_text(
            "❌ Terjadi kesalahan saat memproses undangan\\.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")],
            ]),
            parse_mode='Markdown',
        )
        return ConversationHandler.END
    back_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")],
    ])
    if result.get('stopped_by_flood'):
        record_invite_flood(phone, int(result.get('flood_wait_seconds') or 0))
        sec = int(result.get('flood_wait_seconds') or 0)
        wait_human = escape_markdown(_format_invite_flood_wait_seconds(sec), version=1)
        inv = result.get('invited', 0)
        im = result.get('invited_mutual', 0)
        inm = result.get('invited_non_mutual', 0)
        mt = result.get('mutual_total', 0)
        nmt = result.get('non_mutual_total', 0)
        gl = escape_markdown(str(result.get('group_label') or ''), version=1)
        lines = [
            "⚠️ **Undangan dihentikan: limit Telegram (FloodWait panjang)**",
            "",
            f"Undangan dihentikan karena FloodWait **\\> 5 menit** \\(\\>300 detik\\)\\.",
            "",
            f"⏳ Perkiraan tunggu untuk akun ini: **~{wait_human}**",
            f"\\(`FloodWait` sekitar **{sec}** detik\\)",
            "",
            f"📱 Session: `{escape_markdown(phone, version=1)}`",
            f"👥 Grup: `{gl}`",
            "",
            f"✅ Terundang sebelum limit: **{inv}** kontak",
            f"   • Mutual: **{im}** / **{mt}**",
            f"   • Non\\-mutual: **{inm}** / **{nmt}**",
        ]
        samples = result.get('failed_sample') or []
        if samples:
            lines.append("")
            lines.append("Contoh error:")
            for uid, msg in samples[:5]:
                lines.append(f"• `{uid}`: {escape_markdown(msg, version=1)}")
        await status_msg.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")],
                [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")],
            ]),
            parse_mode='Markdown',
        )
        return ConversationHandler.END
    if not result.get('ok'):
        err = escape_markdown(str(result.get('error') or 'Gagal'), version=1)
        await status_msg.edit_text(
            f"❌ **Invite kontak gagal**\n\n{err}",
            reply_markup=back_kb,
            parse_mode='Markdown',
        )
        return ConversationHandler.END
    clear_invite_flood(phone)
    gl = escape_markdown(str(result.get('group_label') or ''), version=1)
    total = result.get('total_contacts', 0)
    inv = result.get('invited', 0)
    fail = result.get('failed', 0)
    samples = result.get('failed_sample') or []
    im = result.get('invited_mutual', 0)
    inm = result.get('invited_non_mutual', 0)
    mt = result.get('mutual_total', 0)
    nmt = result.get('non_mutual_total', 0)
    lines = [
        "✅ **Selesai invite kontak**",
        "",
        f"📱 Session: `{escape_markdown(phone, version=1)}`",
        f"👥 Grup: `{gl}`",
        f"📇 Kontak diproses: **{total}**",
        f"   • Mutual: **{mt}** → terundang **{im}**",
        f"   • Non\\-mutual: **{nmt}** → terundang **{inm}**",
        f"✅ Total berhasil undang: **{inv}**",
        f"❌ Gagal / tidak terundang: **{fail}**",
    ]
    if samples:
        lines.append("")
        lines.append("Contoh error:")
        for uid, msg in samples[:5]:
            lines.append(f"• `{uid}`: {escape_markdown(msg, version=1)}")
    await status_msg.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")],
            [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")],
        ]),
        parse_mode='Markdown',
    )
    return ConversationHandler.END


async def invite_contacts_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('invite_contacts_phone', None)
    if update.message:
        await update.message.reply_text("❌ Invite kontak dibatalkan.")
    return ConversationHandler.END


async def invite_contacts_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('invite_contacts_phone', None)
    await query.edit_message_text("❌ Invite kontak dibatalkan.")
    return ConversationHandler.END


def get_invite_contacts_conversation_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(invite_contacts_start, pattern=r"^invite_contacts:.+"),
        ],
        states={
            INVITE_CONTACTS_ASK_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, invite_contacts_receive_link),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", invite_contacts_cancel),
            CallbackQueryHandler(invite_contacts_cancel_cb, pattern=r"^cancel_invite_contacts$"),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )


async def join_session_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur gabung grup untuk session yang dipilih."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ **Akses Ditolak**", parse_mode="Markdown")
        return ConversationHandler.END
    data = query.data or ""
    prefix = "join_session_group:"
    if not data.startswith(prefix):
        return ConversationHandler.END
    phone = data[len(prefix) :]
    if not phone:
        await query.edit_message_text("❌ Session tidak valid.")
        return ConversationHandler.END
    context.user_data["join_session_group_phone"] = phone
    phone_esc = escape_markdown(phone, version=1)
    await query.edit_message_text(
        "➕ **Gabung ke grup**\n\n"
        f"📱 Session: `{phone_esc}`\n\n"
        "Kirim **link undangan** \\(`https://t.me/+…`\\) atau **@username** / URL publik, contoh:\n"
        "• `https://t.me/+xxxxxxxx`\n"
        "• `https://t.me/namaGrup`\n"
        "• `@namaGrup`\n\n"
        "Akun session akan **bergabung** ke grup \\(`Join` / terima undangan\\) jika belum anggota\\.\n\n"
        "/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Batal", callback_data="cancel_join_session_group")]],
        ),
    )
    return JOIN_SESSION_GROUP_ASK_LINK


async def join_session_group_receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Akses ditolak.")
        return ConversationHandler.END
    phone = context.user_data.get("join_session_group_phone")
    if not phone:
        await update.message.reply_text("❌ Sesi tidak ditemukan. Ulangi dari menu session.")
        return ConversationHandler.END
    link = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    status_msg = await update.message.reply_text(
        "⏳ Memproses bergabung ke grup\\.\\.\\.",
        parse_mode="Markdown",
    )
    auth = TelethonAuth(phone)
    result = None
    try:
        if not auth.is_session_exists():
            await status_msg.edit_text("❌ File session tidak ditemukan.")
            return ConversationHandler.END
        if not await auth.connect():
            await status_msg.edit_text(
                "❌ Session belum login\\. Gunakan `/login` untuk mengotentikasi ulang\\.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END
        result = await auth.join_group_with_link(link)
    finally:
        try:
            await auth.disconnect()
        except Exception:
            pass

    context.user_data.pop("join_session_group_phone", None)

    back_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")],
            [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")],
        ]
    )
    gl = escape_markdown(str((result or {}).get("group_label") or ""), version=1)

    if result is None or not result.get("ok"):
        err = escape_markdown(str((result or {}).get("error") or "Gagal"), version=1)
        await status_msg.edit_text(
            f"❌ **Gabung grup gagal**\n\n{err}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")]],
            ),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if result.get("already_member"):
        await status_msg.edit_text(
            f"✅ Akun **sudah** merupakan anggota grup ini\\.\n\n"
            f"👥 Grup: `{gl}`\n"
            f"📱 Session: `{escape_markdown(phone, version=1)}`",
            reply_markup=back_kb,
            parse_mode="Markdown",
        )
    else:
        await status_msg.edit_text(
            f"✅ **Berhasil bergabung** ke grup\\.\n\n"
            f"👥 Grup: `{gl}`\n"
            f"📱 Session: `{escape_markdown(phone, version=1)}`",
            reply_markup=back_kb,
            parse_mode="Markdown",
        )
    return ConversationHandler.END


async def join_session_group_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("join_session_group_phone", None)
    if update.message:
        await update.message.reply_text("❌ Gabung grup dibatalkan.")
    return ConversationHandler.END


async def join_session_group_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("join_session_group_phone", None)
    await query.edit_message_text("❌ Gabung grup dibatalkan.")
    return ConversationHandler.END


def get_join_session_group_conversation_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(join_session_group_start, pattern=r"^join_session_group:.+"),
        ],
        states={
            JOIN_SESSION_GROUP_ASK_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, join_session_group_receive_link),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", join_session_group_cancel),
            CallbackQueryHandler(
                join_session_group_cancel_cb, pattern=r"^cancel_join_session_group$"
            ),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )


def _list_all_session_phones() -> list:
    """Nomor/session id dari file .session sesuai urutan kemunculan pertama.

    Urutan disimpan di `session_order.json` (insertion order):
    - Session lama tetap di posisi semula.
    - Session baru selalu di-append di akhir.
    - Session yang file-nya dihapus → hilang dari urutan.
    """
    return get_ordered_session_phones()


def _format_remaining_compact(sec: int) -> str:
    """Format kompak sisa FloodWait untuk label tombol: '45s' / '12m' / '3h' / '2d'."""
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _list_mark_with_remaining(phone: str) -> str:
    """✅ jika aman, ❌ <sisa> jika masih FloodWait aktif (dibaca real-time dari disk)."""
    st = get_invite_flood_status(phone)
    if st.get("active"):
        rem = int(st.get("remaining_sec") or 0)
        return f"❌ {_format_remaining_compact(rem)}"
    return "✅"


async def manage_join_all_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dari menu manage: undang semua session ke satu grup (link/username)."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ **Akses Ditolak**", parse_mode="Markdown")
        return ConversationHandler.END
    phones = _list_all_session_phones()
    if not phones:
        await query.edit_message_text(
            "❌ **Belum ada session**\\. Login dulu lewat `/login`\\.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Menu manage", callback_data="back_to_menu")]],
            ),
        )
        return ConversationHandler.END
    n = len(phones)
    await query.edit_message_text(
        "➕ **Join Grup — semua session**\n\n"
        f"📊 Akan memproses **{n}** session yang ada di folder `sessions`\\.\n\n"
        "Kirim **link undangan** \\(`https://t.me/+…`\\) atau **@username** / URL publik grup\\.\n"
        "Setiap akun session akan **bergabung** ke grup tersebut \\(bergiliran\\)\\.\n\n"
        "/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Batal", callback_data="cancel_manage_join_all_group")]],
        ),
    )
    return MANAGE_JOIN_ALL_GROUP_ASK_LINK


async def manage_join_all_group_receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Akses ditolak.")
        return ConversationHandler.END
    link = (update.message.text or "").strip()
    phones = _list_all_session_phones()
    if not phones:
        await update.message.reply_text("❌ Tidak ada session.")
        return ConversationHandler.END
    try:
        await update.message.delete()
    except Exception:
        pass
    total = len(phones)
    status_msg = await update.message.reply_text(
        f"⏳ Join grup untuk **{total}** session\\.\\.\\. `0/{total}`",
        parse_mode="Markdown",
    )
    results = []
    for i, phone in enumerate(phones):
        try:
            await status_msg.edit_text(
                f"⏳ Memproses `{escape_markdown(phone, version=1)}` \\({i + 1}/{total}\\)\\.\\.\\.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        auth = TelethonAuth(phone)
        row = {"phone": phone, "ok": False, "already_member": False, "error": None, "group_label": None}
        try:
            if not auth.is_session_exists():
                row["error"] = "File session tidak ada"
                results.append(row)
                continue
            if not await auth.connect():
                row["error"] = "Belum login (/login)"
                results.append(row)
                continue
            r = await auth.join_group_with_link(link)
            row["ok"] = bool(r.get("ok"))
            row["already_member"] = bool(r.get("already_member"))
            row["error"] = r.get("error")
            row["group_label"] = r.get("group_label")
            results.append(row)
        finally:
            try:
                await auth.disconnect()
            except Exception:
                pass
        await asyncio.sleep(0.2)

    ok_list = [x for x in results if x.get("ok")]
    fail_list = [x for x in results if not x.get("ok")]
    already = sum(1 for x in ok_list if x.get("already_member"))
    new_join = len(ok_list) - already
    group_label = None
    for x in results:
        if x.get("group_label"):
            group_label = x["group_label"]
            break

    lines = [
        "✅ **Join Grup — semua session selesai**",
        "",
    ]
    if group_label:
        lines.append(f"👥 Grup: `{escape_markdown(str(group_label), version=1)}`")
        lines.append("")
    lines.extend(
        [
            f"📊 Diproses: **{len(results)}** session",
            f"✅ OK: **{len(ok_list)}** \\(anggota lama **{already}**, baru gabung **{new_join}**\\)",
            f"❌ Gagal: **{len(fail_list)}**",
        ]
    )
    if fail_list:
        lines.append("")
        lines.append("**Contoh gagal:**")
        for x in fail_list[:18]:
            pe = escape_markdown(x["phone"], version=1)
            ee = escape_markdown(str(x.get("error") or "")[:90], version=1)
            lines.append(f"• `{pe}` — {ee}")

    text = "\n".join(lines)
    if len(text) > 4090:
        text = text[:4080] + "\n\\.\\.\\.\\.\\.\\."

    await status_msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔙 Menu manage", callback_data="back_to_menu")],
                [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")],
            ],
        ),
    )
    return ConversationHandler.END


async def manage_join_all_group_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("❌ Join grup semua session dibatalkan.")
    return ConversationHandler.END


async def manage_join_all_group_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Join grup semua session dibatalkan.")
    return ConversationHandler.END


def get_manage_join_all_group_conversation_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                manage_join_all_group_start, pattern=r"^manage_join_all_group$"
            ),
        ],
        states={
            MANAGE_JOIN_ALL_GROUP_ASK_LINK: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, manage_join_all_group_receive_link
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", manage_join_all_group_cancel),
            CallbackQueryHandler(
                manage_join_all_group_cancel_cb,
                pattern=r"^cancel_manage_join_all_group$",
            ),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )


def _manage_scrape_all_filter_labels() -> dict:
    return {7: "≤7 hari", 14: "≤14 hari", 30: "≤30 hari (~1 bulan)"}


def _manage_scrape_all_filter_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🕐 ≤7 hari", callback_data="mscrape_f:7"),
                InlineKeyboardButton("📅 ≤14 hari", callback_data="mscrape_f:14"),
            ],
            [
                InlineKeyboardButton("📆 ≤30 hari (~1 bln)", callback_data="mscrape_f:30"),
            ],
            [InlineKeyboardButton("❌ Batal", callback_data="cancel_manage_scrape_all")],
        ]
    )


def _clear_manage_scrape_all_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in (
        "manage_scrape_all_phones",
        "manage_scrape_all_per_session_cap",
        "manage_scrape_all_source",
        "manage_scrape_all_target",
    ):
        context.user_data.pop(k, None)


def _parse_manage_scrape_session_range(text: str, all_phones: list) -> tuple:
    """Pilih subset session dari nomor urut 1..N (urutan sama seperti folder terurut).
    Kembalikan (subset_list|None, error_message|None)."""
    total = len(all_phones)
    raw = (text or "").strip()
    t = raw.lower()
    if not t or t in ("all", "semua", "*"):
        return list(all_phones), None
    compact = re.sub(r"\s+", "", raw)
    m = re.match(r"^(\d+)[-–](\d+)$", compact)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a < 1 or b < 1 or a > total or b > total or a > b:
            return None, f"Rentang harus 1–{total} dan awal ≤ akhir (Anda punya {total} session)."
        return all_phones[a - 1 : b], None
    m2 = re.match(r"^(\d+)$", compact)
    if m2:
        x = int(m2.group(1))
        if x < 1 or x > total:
            return None, f"Nomor harus antara 1 dan {total}."
        return [all_phones[x - 1]], None
    return (
        None,
        "Format tidak valid. Contoh: `1-100`, `100-200`, `50` (satu session), atau `semua`.",
    )


async def manage_scrape_all_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ **Akses Ditolak**", parse_mode="Markdown")
        return ConversationHandler.END
    phones = _list_all_session_phones()
    if not phones:
        await query.edit_message_text(
            "❌ **Belum ada session**\\. Login lewat `/login` dulu\\.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Menu manage", callback_data="back_to_menu")]],
            ),
        )
        return ConversationHandler.END
    _clear_manage_scrape_all_data(context)
    n = len(phones)
    await query.edit_message_text(
        "📥 **Scrape Grup \\(multi\\-session\\)**\n\n"
        f"📊 Total **{n}** file session \\(urut: **1** \\= pertama, **{n}** \\= terakhir\\)\\.\n\n"
        "**Langkah 1/5:** Pilih session yang **dipakai** untuk gabung\\+scrape\\+undang:\n"
        "• `1-100` — nomor **1** sampai **100** \\(inklusif\\)\n"
        "• `100-200` — rentang lain\n"
        "• `5` — **hanya** session nomor 5\n"
        "• `semua` — semua session\n\n"
        f"Tiap **gelombang**: tiap session aktif mengundang sampai **{MANAGE_SCRAPE_INVITES_PER_ROUND}** "
        f"anggota **paralel** antar session\\. FloodWait \\>5 menit hanya menendang session yang kena, "
        f"sisanya tetap berjalan\\.\n\n"
        "/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Batal", callback_data="cancel_manage_scrape_all")]],
        ),
    )
    return MANAGE_SCRAPE_ALL_RANGE


async def manage_scrape_all_receive_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Akses ditolak.")
        return ConversationHandler.END
    all_phones = _list_all_session_phones()
    if not all_phones:
        await update.message.reply_text("❌ Tidak ada session.")
        return ConversationHandler.END
    text_in = (update.message.text or "").strip()
    subset, err = _parse_manage_scrape_session_range(text_in, all_phones)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return MANAGE_SCRAPE_ALL_RANGE
    context.user_data["manage_scrape_all_phones"] = subset
    n_all = len(all_phones)
    n_sub = len(subset)
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ **{n_sub}** session dipilih \\(dari **{n_all}** total\\)\\.\n\n"
        "**Langkah 2/5:** Kirim **batas member per session** "
        "\\(berapa banyak anggota yang boleh **berhasil** diundang oleh **tiap** session\\)\\.\n\n"
        f"• Angka **1**–**{MANAGE_SCRAPE_PER_SESSION_CAP_MAX}** \\(disarankan **3**–**10** untuk menghindari FloodWait\\)\n"
        "• `0` atau `tanpa` — **tidak dibatasi** \\(berhenti hanya saat antrian habis / FloodWait\\)\n\n"
        f"Contoh: kirim `5` → tiap session **maksimal 5** anggota berhasil diundang, "
        f"lalu session itu otomatis berhenti\\.\n\n"
        "/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Batal", callback_data="cancel_manage_scrape_all")]],
        ),
    )
    return MANAGE_SCRAPE_ALL_PER_SESSION_CAP


async def manage_scrape_all_receive_per_session_cap(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Akses ditolak.")
        return ConversationHandler.END
    if not context.user_data.get("manage_scrape_all_phones"):
        await update.message.reply_text("❌ Pilihan session hilang. Ulangi dari menu.")
        return ConversationHandler.END
    text_in = (update.message.text or "").strip().lower()
    cap = None
    if text_in in ("0", "tanpa", "tanpa batas", "unlimited", "no limit", "semua"):
        cap = 0
    else:
        try:
            cap = int(text_in)
        except ValueError:
            await update.message.reply_text(
                f"❌ Masukkan angka 1–{MANAGE_SCRAPE_PER_SESSION_CAP_MAX} atau `0`/`tanpa` untuk tidak dibatasi.",
                parse_mode="Markdown",
            )
            return MANAGE_SCRAPE_ALL_PER_SESSION_CAP
    if cap < 0:
        cap = 0
    if cap > MANAGE_SCRAPE_PER_SESSION_CAP_MAX:
        cap = MANAGE_SCRAPE_PER_SESSION_CAP_MAX
    context.user_data["manage_scrape_all_per_session_cap"] = cap
    try:
        await update.message.delete()
    except Exception:
        pass
    cap_lbl = f"**{cap}** anggota/sesi" if cap > 0 else "**tanpa batas**"
    await update.message.reply_text(
        f"✅ Batas per session: {cap_lbl}\\.\n\n"
        "**Langkah 3/5:** Kirim **link/username GRUP SUMBER** \\(anggota diambil dari sini\\)\\.\n\n"
        "/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Batal", callback_data="cancel_manage_scrape_all")]],
        ),
    )
    return MANAGE_SCRAPE_ALL_SOURCE


async def manage_scrape_all_receive_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Akses ditolak.")
        return ConversationHandler.END
    if not context.user_data.get("manage_scrape_all_phones"):
        await update.message.reply_text("❌ Rentang session hilang. Ulangi dari menu.")
        return ConversationHandler.END
    src = (update.message.text or "").strip()
    if not src:
        await update.message.reply_text("❌ Kirim link atau @username grup sumber.")
        return MANAGE_SCRAPE_ALL_SOURCE
    context.user_data["manage_scrape_all_source"] = src
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text(
        "**Langkah 4/5:** Kirim **link/username GRUP TUJUAN** \\(undangan / @publik\\)\\.\n\n"
        "/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Batal", callback_data="cancel_manage_scrape_all")]],
        ),
    )
    return MANAGE_SCRAPE_ALL_TARGET


async def manage_scrape_all_receive_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Akses ditolak.")
        return ConversationHandler.END
    if not context.user_data.get("manage_scrape_all_source") or not context.user_data.get(
        "manage_scrape_all_phones"
    ):
        await update.message.reply_text("❌ Ulangi dari menu: data hilang.")
        return ConversationHandler.END
    tgt = (update.message.text or "").strip()
    if not tgt:
        await update.message.reply_text("❌ Kirim link atau @username grup tujuan.")
        return MANAGE_SCRAPE_ALL_TARGET
    context.user_data["manage_scrape_all_target"] = tgt
    try:
        await update.message.delete()
    except Exception:
        pass
    lbl = _manage_scrape_all_filter_labels()
    fl = "\n".join(
        [
            f"• **{k} hari:** {escape_markdown(v, version=1)}" for k, v in sorted(lbl.items())
        ]
    )
    await update.message.reply_text(
        "**Langkah 5/5:** Pilih **filter anggota** yang boleh diundang \\(berdasarkan last seen Telegram\\)\\.\n\n"
        "⚠️ Akun yang **menyembunyikan** last seen tidak akan ikut\\.\n\n"
        f"{fl}\n\n"
        "/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=_manage_scrape_all_filter_keyboard(),
    )
    return MANAGE_SCRAPE_ALL_FILTER


async def manage_scrape_all_pick_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m = re.match(r"^mscrape_f:(\d+)$", query.data or "")
    if not m:
        return MANAGE_SCRAPE_ALL_FILTER
    days = int(m.group(1))
    if days not in (7, 14, 30):
        return MANAGE_SCRAPE_ALL_FILTER
    src = context.user_data.pop("manage_scrape_all_source", None)
    tgt = context.user_data.pop("manage_scrape_all_target", None)
    phones_sel = context.user_data.pop("manage_scrape_all_phones", None)
    per_session_cap = context.user_data.pop("manage_scrape_all_per_session_cap", 0)
    if not src or not tgt or not phones_sel:
        await query.edit_message_text(
            "❌ Data alur hilang\\. Ulangi dari menu **Scrape Grup \\(semua\\)**\\.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    flabel = _manage_scrape_all_filter_labels().get(days, str(days))
    cap_lbl = f"{per_session_cap}/session" if per_session_cap > 0 else "tanpa batas"
    await query.edit_message_text(
        f"⏳ **Memproses** \\(filter: {escape_markdown(flabel, version=1)}, "
        f"batas: {escape_markdown(cap_lbl, version=1)}\\)\\.\\.\\.\n\n"
        f"**{len(phones_sel)}** session terpilih\\. Progres akan tampil di pesan ini "
        f"\\(gabung paralel → scrape → undangan paralel\\)\\.",
        parse_mode="Markdown",
    )
    await _run_manage_scrape_all_sessions_core(
        query, src, tgt, days, phones_sel, per_session_cap=per_session_cap
    )
    return ConversationHandler.END


async def _run_manage_scrape_all_sessions_core(
    query,
    source_link: str,
    target_link: str,
    last_seen_days: int,
    phones=None,
    per_session_cap: int = 0,
):
    """Orkestrasi: gabung PARALEL → scrape → undangan PARALEL antar session.

    Setiap session **resolve entity sendiri** (access_hash valid per akun) untuk
    menghindari error 'Invalid channel object' / 'Invalid object ID for a user'.
    FloodWait >5 menit hanya menendang **session yang kena**, sisanya jalan terus.
    """
    back_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 Menu manage", callback_data="back_to_menu")],
            [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")],
        ]
    )

    async def _safe_edit(text: str, **kwargs):
        try:
            await query.edit_message_text(text, **kwargs)
        except Exception:
            try:
                await query.message.reply_text(text, **kwargs)
            except Exception:
                pass

    async def _progress_edit(phase: str, body_lines: list):
        body = "\n".join(body_lines)
        await _safe_edit(f"⏳ **{phase}**\n\n{body}", parse_mode="Markdown")

    if phones is None:
        phones = _list_all_session_phones()
    if not phones:
        await _safe_edit("❌ Tidak ada session.", reply_markup=back_kb, parse_mode="Markdown")
        return

    # 1) Resolve label grup pakai session pertama yang bisa login (untuk validasi & label).
    src_ent_label = tgt_ent_label = None
    resolve_err = "Tidak ada session yang bisa login."
    for phone in phones:
        auth = TelethonAuth(phone)
        try:
            if not auth.is_session_exists() or not await auth.connect():
                continue
            s, e1 = await auth.resolve_group_entity_for_invite(source_link)
            if e1:
                resolve_err = e1
                continue
            t, e2 = await auth.resolve_group_entity_for_invite(target_link)
            if e2:
                resolve_err = e2
                continue
            src_ent_label, tgt_ent_label = s, t
            resolve_err = None
            break
        finally:
            try:
                await auth.disconnect()
            except Exception:
                pass

    if src_ent_label is None or tgt_ent_label is None or resolve_err:
        err_e = escape_markdown(str(resolve_err or "Gagal"), version=1)
        await _safe_edit(
            f"❌ **Resolve grup gagal**\n\n{err_e}",
            reply_markup=back_kb,
            parse_mode="Markdown",
        )
        return

    if TelethonAuth._same_tg_group(src_ent_label, tgt_ent_label):
        await _safe_edit(
            "❌ Grup **sumber** dan **tujuan** tidak boleh sama\\.",
            reply_markup=back_kb,
            parse_mode="Markdown",
        )
        return

    src_lbl = (
        getattr(src_ent_label, "title", None)
        or getattr(src_ent_label, "username", "")
        or "sumber"
    )
    tgt_lbl = (
        getattr(tgt_ent_label, "title", None)
        or getattr(tgt_ent_label, "username", "")
        or "tujuan"
    )
    fl_human_progress = escape_markdown(
        _manage_scrape_all_filter_labels().get(last_seen_days, str(last_seen_days)), version=1
    )

    # 2) Gabung paralel + scrape per session (mengisi peer cache yang valid).
    # Karena StringSession Telethon TIDAK menyimpan peer cache ke disk, kita harus
    # menjaga client tetap connected setelah gabung sampai undangan selesai.
    # Tiap session juga melakukan iter_participants(loc_src) untuk mendapatkan
    # objek User dengan access_hash valid bagi akun itu sendiri.
    async def _join_one_session(phone: str):
        auth = TelethonAuth(phone)
        opened = False
        try:
            if not auth.is_session_exists():
                return phone, False, f"`{escape_markdown(phone, version=1)}` — tidak ada file", None
            if not await auth.connect():
                return phone, False, f"`{escape_markdown(phone, version=1)}` — belum login", None
            opened = True
            loc_src, e1 = await auth.resolve_group_entity_for_invite(source_link)
            loc_tgt, e2 = await auth.resolve_group_entity_for_invite(target_link)
            if e1 or e2:
                msg = (e1 or e2 or "resolve gagal")[:120]
                return (
                    phone,
                    False,
                    f"`{escape_markdown(phone, version=1)}` — {escape_markdown(msg, version=1)}",
                    None,
                )
            ok_s, es = await auth._ensure_session_joined_target_group(loc_src)
            ok_t, et = await auth._ensure_session_joined_target_group(loc_tgt)
            if not (ok_s and ok_t):
                msg = (es or et or "gagal join")[:80]
                return (
                    phone,
                    False,
                    f"`{escape_markdown(phone, version=1)}` — {escape_markdown(msg, version=1)}",
                    None,
                )
            users_by_id, prime_mode, prime_note = await auth._collect_user_pool(
                loc_src, participants_limit=5000
            )
            return phone, True, None, {
                "auth": auth,
                "src": loc_src,
                "tgt": loc_tgt,
                "users": users_by_id,
                "prime_mode": prime_mode,
                "prime_note": prime_note,
            }
        except Exception as ex:
            if opened:
                try:
                    await auth.disconnect()
                except Exception:
                    pass
            return (
                phone,
                False,
                f"`{escape_markdown(phone, version=1)}` — {escape_markdown(str(ex)[:80], version=1)}",
                None,
            )

    n_ph = len(phones)
    await _progress_edit(
        "Gabung ke grup sumber & tujuan",
        [
            f"Menjalankan **{n_ph}** session **paralel** \\(satu task per nomor\\)\\.\\.\\.",
            "Termasuk **memuat peer cache** \\(daftar anggota grup sumber\\) agar undangan lintas\\-session bekerja\\.",
            "Mohon tunggu\\.\\.\\.",
        ],
    )
    gather_res = await asyncio.gather(
        *[_join_one_session(p) for p in phones],
        return_exceptions=True,
    )
    phones_ok = []
    join_fail_snippets = []
    auth_pool = {}  # phone -> {"auth", "src", "tgt", "users"}
    for item in gather_res:
        if isinstance(item, Exception):
            continue
        phone, ok, snip, payload = item
        if ok and payload:
            phones_ok.append(phone)
            auth_pool[phone] = payload
        elif snip:
            join_fail_snippets.append(snip)

    async def _disconnect_pool():
        for phone in list(auth_pool.keys()):
            entry = auth_pool.get(phone)
            if not entry:
                continue
            try:
                await entry["auth"].disconnect()
            except Exception:
                pass

    await _progress_edit(
        "Gabung ke grup sumber & tujuan",
        [
            f"**Selesai**: **{len(phones_ok)}** / **{n_ph}** session lolos gabung\\.",
        ],
    )

    if not phones_ok:
        jf = "\n".join(join_fail_snippets[:15])
        await _disconnect_pool()
        await _safe_edit(
            "❌ **Tidak ada session** yang berhasil masuk **sumber** dan **tujuan**\\.\n\n" + jf,
            reply_markup=back_kb,
            parse_mode="Markdown",
        )
        return

    # 3) Scrape: pakai session pertama yang sudah terkoneksi (tidak perlu reconnect).
    scraper = phones_ok[0]
    users_list = []
    await _progress_edit(
        "Scrape anggota grup sumber",
        [
            f"Filter: **{fl_human_progress}**",
            f"Session **scrape**: `{escape_markdown(scraper, version=1)}`",
            "Membaca daftar anggota \\(memakai client yang sudah connected\\)\\.\\.\\.",
        ],
    )
    try:
        entry_s = auth_pool[scraper]
        users_list, scrape_mode, scrape_note = await entry_s[
            "auth"
        ].collect_scraped_users_filtered_v2(entry_s["src"], last_seen_days)
    except Exception as ex_sc:
        await _disconnect_pool()
        await _safe_edit(
            f"❌ **Scrape gagal**: {escape_markdown(str(ex_sc)[:200], version=1)}",
            reply_markup=back_kb,
            parse_mode="Markdown",
        )
        return

    hidden_members_note = None
    if scrape_mode in ("messages_fallback", "mixed"):
        hidden_members_note = (
            f"ℹ️ Grup sumber **menyembunyikan member**\\. "
            f"Bot beralih ke **scrape dari riwayat pesan** \\(maks **1000** pesan terakhir\\)\\. "
            f"Hanya member yang **aktif chat** yang terjangkau\\."
        )

    if not users_list:
        fl = escape_markdown(_manage_scrape_all_filter_labels().get(last_seen_days, ""), version=1)
        await _disconnect_pool()
        await _safe_edit(
            f"⚠️ **Tidak ada anggota** yang cocok filter **{fl}** di grup sumber "
            "\\(last seen disembunyikan / tidak ada yang cocok\\)\\.\n\n"
            f"📤 `{escape_markdown(str(src_lbl), version=1)}`\n"
            f"📥 `{escape_markdown(str(tgt_lbl), version=1)}`\n"
            f"✅ Session siap: **{len(phones_ok)}**",
            reply_markup=back_kb,
            parse_mode="Markdown",
        )
        return

    progress_after_scrape = [
        f"Selesai: **{len(users_list)}** anggota lolos filter \\(calon undangan\\)\\.",
    ]
    if hidden_members_note:
        progress_after_scrape.append(hidden_members_note)
    progress_after_scrape.append("Lanjut: undangan **paralel** antar session\\.\\.\\.")
    await _progress_edit("Scrape anggota grup sumber", progress_after_scrape)

    # 4) Undangan paralel antar session. Tiap session resolve tujuan SENDIRI.
    q = deque(users_list)
    total_invited = 0
    rpc_fail_lines = []
    flooded_skip_notes = []
    round_notes = []
    phones_active = set(phones_ok)
    wave_idx = 0
    # Anti-infinite-loop: drop user setelah N kali gagal (semua chunk dikembalikan tanpa progres)
    user_fail_count = {}
    dropped_due_to_fails = 0
    DROP_USER_AFTER_FAILS = 3
    no_progress_streak = 0
    NO_PROGRESS_BREAK = 5
    last_wave_note = None
    # Dua counter terpisah:
    # - `session_attempted_count`: hard-cap budget (cara hitung: jumlah user yang
    #   benar-benar dikirim ke Telegram, dihitung saat chunk dirakit & di-refund
    #   bila chunk dikembalikan oleh wave). Ini yang dipakai untuk **enforce cap**.
    # - `session_invited_count`: jumlah undangan **sukses** menurut laporan Telegram
    #   (UpdateChannelParticipant), dipakai hanya untuk tampilan ringkasan.
    session_attempted_count = {p: 0 for p in phones_ok}
    session_invited_count = {p: 0 for p in phones_ok}
    session_capped_phones = set()  # session yang sudah mencapai cap

    async def _invite_wave_task(phone: str, chunk: list):
        # chunk: list[User] dari scraper. Kita lookup User-versi-phone-ini di auth_pool[phone]["users"]
        # supaya access_hash valid untuk client yang akan memanggil InviteToChannelRequest.
        out = {
            "phone": phone,
            "invited": 0,
            "flood_wait_seconds": None,
            "return_chunk": [],
            "note": None,
            "failed_sample": [],
        }
        entry = auth_pool.get(phone)
        if not entry:
            out["return_chunk"] = list(chunk)
            out["note"] = "session tidak aktif di pool"
            return out
        auth = entry["auth"]
        loc_tgt = entry["tgt"]
        user_map = entry["users"]
        users_for_this = []
        requeue_uncached = []  # scraper-User yang tidak ada di peer cache phone ini
        scraper_by_id = {}     # uid -> scraper User (untuk balikin ke antrian saat FloodWait)
        for u in chunk:
            uid = getattr(u, "id", None)
            if uid is None:
                requeue_uncached.append(u)
                continue
            scraper_by_id[uid] = u
            mapped = user_map.get(uid)
            if mapped is not None:
                users_for_this.append(mapped)
            else:
                requeue_uncached.append(u)
        if not users_for_this:
            out["return_chunk"] = list(chunk)
            out["note"] = "tidak ada user terkait di peer cache session ini"
            return out
        try:
            failed_s = []
            sub = await auth.invite_user_chunk_to_target(
                loc_tgt, users_for_this, failed_s, skip_hydrate=True
            )
            out["invited"] = int(sub.get("invited") or 0)
            out["flood_wait_seconds"] = sub.get("flood_wait_seconds")
            out["failed_sample"] = list(sub.get("failed_sample") or [])
            rem_local = sub.get("remaining_users") or []  # User-versi-phone-ini
            rem_scraper = []
            for u in rem_local:
                uid = getattr(u, "id", None)
                back = scraper_by_id.get(uid) if uid is not None else None
                if back is not None:
                    rem_scraper.append(back)
            out["return_chunk"] = list(requeue_uncached) + rem_scraper
            return out
        except Exception as ex:
            out["return_chunk"] = list(chunk)
            out["note"] = f"error: {str(ex)[:60]}"
            return out

    while q and phones_active:
        active_list = [p for p in phones_ok if p in phones_active]
        if not active_list:
            break
        assignments = []
        for phone in active_list:
            # Hitung sisa kuota per session jika cap aktif. Pakai counter "attempted"
            # supaya cap menjadi HARD limit, terlepas dari apakah Telegram melaporkan
            # sukses lewat update (kadang under-reported).
            if per_session_cap > 0:
                remaining_cap = per_session_cap - session_attempted_count.get(phone, 0)
                if remaining_cap <= 0:
                    phones_active.discard(phone)
                    session_capped_phones.add(phone)
                    continue
                limit_this_wave = min(MANAGE_SCRAPE_INVITES_PER_ROUND, remaining_cap)
            else:
                limit_this_wave = MANAGE_SCRAPE_INVITES_PER_ROUND
            chunk = []
            for _ in range(limit_this_wave):
                if q:
                    chunk.append(q.popleft())
            if chunk:
                assignments.append((phone, chunk))
                # Reserve budget upfront. Bagian yang dikembalikan oleh wave (mis.
                # tidak ada di peer cache / FloodWait stop) akan di-refund di bawah.
                if per_session_cap > 0:
                    session_attempted_count[phone] = (
                        session_attempted_count.get(phone, 0) + len(chunk)
                    )
        if not assignments:
            break

        wave_idx += 1
        results = await asyncio.gather(
            *[_invite_wave_task(p, c) for p, c in assignments],
            return_exceptions=True,
        )

        wave_invited = 0
        wave_flood = []
        wave_returned_users = []  # users yang dikembalikan ke antrian (untuk hitung kegagalan)
        for i, r in enumerate(results):
            phone, chunk = assignments[i]
            if isinstance(r, Exception):
                wave_returned_users.extend(chunk)
                round_notes.append(f"`{phone}`: {escape_markdown(str(r)[:40], version=1)}")
                last_wave_note = f"{phone}: {str(r)[:60]}"
                continue
            ret_chunk = r.get("return_chunk") or []
            if ret_chunk:
                wave_returned_users.extend(ret_chunk)
            # Refund budget per chunk yang dikembalikan (tidak benar-benar
            # dikirim ke Telegram), agar cap hanya menahan user yang TRULY sent.
            rphone = r.get("phone") or phone
            if per_session_cap > 0 and ret_chunk:
                session_attempted_count[rphone] = max(
                    0,
                    session_attempted_count.get(rphone, 0) - len(ret_chunk),
                )
            if r.get("note"):
                round_notes.append(
                    f"`{escape_markdown(str(r['phone']), version=1)}`: "
                    f"{escape_markdown(str(r['note']), version=1)}"
                )
                last_wave_note = f"{r['phone']}: {r['note']}"
            inv = int(r.get("invited") or 0)
            wave_invited += inv
            total_invited += inv
            if inv > 0 and rphone:
                session_invited_count[rphone] = (
                    session_invited_count.get(rphone, 0) + inv
                )
            # Cek cap berdasar budget (HARD limit), bukan reported invited.
            if (
                per_session_cap > 0
                and rphone
                and session_attempted_count.get(rphone, 0) >= per_session_cap
            ):
                phones_active.discard(rphone)
                session_capped_phones.add(rphone)
            for uid, msg in r.get("failed_sample") or []:
                if len(rpc_fail_lines) < 14:
                    rpc_fail_lines.append(
                        f"uid `{uid}`: {escape_markdown((msg or '')[:72], version=1)}"
                    )
                # User dilaporkan gagal RPC oleh batch — hitung sebagai kegagalan permanen.
                user_fail_count[uid] = user_fail_count.get(uid, 0) + DROP_USER_AFTER_FAILS
            fw = r.get("flood_wait_seconds")
            if fw is not None:
                fp = r.get("phone")
                record_invite_flood(fp, int(fw))
                phones_active.discard(fp)
                wave_flood.append((fp, int(fw)))
                flooded_skip_notes.append(
                    f"`{escape_markdown(str(fp), version=1)}` "
                    f"\\(FloodWait \\~{int(fw)}s \\> {FLOOD_WAIT_ABORT_ABOVE_SEC // 60} m\\) "
                    f"— di\\-skip, lainnya lanjut"
                )

        # Requeue user yang dikembalikan, kecuali sudah gagal terlalu sering.
        for u in reversed(wave_returned_users):
            uid = getattr(u, "id", None)
            if uid is None:
                q.appendleft(u)
                continue
            user_fail_count[uid] = user_fail_count.get(uid, 0) + 1
            if user_fail_count[uid] >= DROP_USER_AFTER_FAILS:
                dropped_due_to_fails += 1
                continue
            q.appendleft(u)

        # Deteksi loop tanpa kemajuan: jika tidak ada undangan & tidak ada drop & tidak ada flood baru
        # untuk beberapa gelombang berturut-turut → keluar agar tidak menghabiskan waktu sia-sia.
        progressed_this_wave = (
            wave_invited > 0
            or bool(wave_flood)
            or dropped_due_to_fails > 0
        )
        if progressed_this_wave:
            no_progress_streak = 0
        else:
            no_progress_streak += 1
        if no_progress_streak >= NO_PROGRESS_BREAK:
            round_notes.append(
                f"⚠️ Berhenti otomatis: **{NO_PROGRESS_BREAK}** gelombang tanpa kemajuan "
                f"\\(undangan 0, tanpa FloodWait baru, antrian sama\\)\\."
            )
            break

        show_prog = (
            wave_idx == 1
            or wave_idx % SCRAPE_ALL_PROGRESS_EVERY_N_WAVES == 0
            or wave_flood
            or not q
            or not phones_active
            or no_progress_streak >= 2
        )
        if show_prog:
            sess_lines = [
                f"• `{escape_markdown(p, version=1)}`" for p, _ in assignments[:12]
            ]
            if len(assignments) > 12:
                sess_lines.append(f"• … **\\+{len(assignments) - 12}** session lain")
            prog = [
                f"**Session yang mengundang di gelombang ini ({len(assignments)}):**",
                "\n".join(sess_lines) if sess_lines else "—",
                f"Undangan di gelombang ini: **{wave_invited}** • **Total terundang:** **{total_invited}**",
                f"**Antrian anggota grup sumber** \\(calon penerima\\): **{len(q)}**",
                f"Session aktif: **{len(phones_active)}** dari **{len(phones_ok)}**",
            ]
            if per_session_cap > 0:
                prog.append(
                    f"🎯 Batas per session: **{per_session_cap}** anggota "
                    f"\\(sudah penuh: **{len(session_capped_phones)}**\\)"
                )
            if dropped_due_to_fails:
                prog.append(
                    f"🗑️ User di\\-drop \\(gagal {DROP_USER_AFTER_FAILS}× di banyak session\\): "
                    f"**{dropped_due_to_fails}**"
                )
            if wave_flood:
                prog.append(
                    f"⏸️ FloodWait baru pada **{len(wave_flood)}** session "
                    f"\\(>{FLOOD_WAIT_ABORT_ABOVE_SEC // 60} menit\\) — di\\-skip"
                )
            if wave_invited == 0 and last_wave_note:
                prog.append(
                    f"⚠️ Catatan terakhir: {escape_markdown(str(last_wave_note)[:120], version=1)}"
                )
            if no_progress_streak >= 2:
                prog.append(
                    f"⏳ Tidak ada kemajuan {no_progress_streak} gelombang berturut\\-turut "
                    f"\\(akan berhenti otomatis di {NO_PROGRESS_BREAK}\\)"
                )
            await _progress_edit(f"Undangan paralel — gelombang **#{wave_idx}**", prog)

        await asyncio.sleep(0.45)

    fl_human = escape_markdown(
        _manage_scrape_all_filter_labels().get(last_seen_days, str(last_seen_days)), version=1
    )
    sl = escape_markdown(str(src_lbl), version=1)
    tl = escape_markdown(str(tgt_lbl), version=1)
    # Budget pesan Telegram = 4096 char; pakai 3900 sebagai aman.
    MSG_BUDGET = 3900

    def _short(s: str, n: int) -> str:
        s = s or ""
        return s if len(s) <= n else (s[: max(0, n - 1)] + "…")

    cap_line = (
        f"🎯 Batas per session: **{per_session_cap}** "
        f"\\(penuh: **{len(session_capped_phones)}**\\)"
        if per_session_cap > 0
        else "🎯 Batas per session: **tanpa batas**"
    )
    header = [
        "✅ **Scrape grup \\(multi\\-session\\) selesai**",
        "",
        f"📤 Sumber: `{_short(sl, 60)}`",
        f"📥 Tujuan: `{_short(tl, 60)}`",
        f"🔎 Filter: **{fl_human}**",
        f"📇 Kandidat scrape: **{len(users_list)}**",
        f"✅ **Terundang total**: **{total_invited}**",
        f"📦 Sisa antrian: **{len(q)}**",
        f"🗑️ Di\\-drop \\(gagal {DROP_USER_AFTER_FAILS}×\\): **{dropped_due_to_fails}**",
        cap_line,
        f"👥 Session aktif: **{len(phones_active)}** / **{len(phones_ok)}** "
        f"\\(dari **{len(phones)}** dipilih\\)",
    ]
    if not phones_active and q:
        if per_session_cap > 0 and len(session_capped_phones) >= len(phones_ok):
            header.append(
                "✅ Semua session **mencapai batas** yang ditetapkan; sisa antrian "
                "tidak diproses lagi\\."
            )
        else:
            header.append("⚠️ **Semua** session tidak aktif; sisa antrian belum selesai\\.")
    if hidden_members_note:
        # `hidden_members_note` sudah pre-escaped untuk Markdown V1.
        header.append(hidden_members_note)

    sections = []  # list[(judul, list_of_lines, max_items)]
    # Ringkasan per-session: nomor → jumlah berhasil
    sess_lines_summary = []
    for ph in phones_ok:
        cnt = session_invited_count.get(ph, 0)
        if cnt > 0:
            mark = " ✅" if ph in session_capped_phones else ""
            sess_lines_summary.append(
                f"`{escape_markdown(ph, version=1)}` → **{cnt}**{mark}"
            )
    if sess_lines_summary:
        sections.append(("📊 Berhasil per session", sess_lines_summary, 10))
    if flooded_skip_notes:
        sections.append(("⏸️ FloodWait \\(di\\-skip\\)", flooded_skip_notes, 5))
    if rpc_fail_lines:
        sections.append(("❌ Gagal RPC \\(sample\\)", rpc_fail_lines, 6))
    if join_fail_snippets:
        sections.append(("🚫 Gabung gagal \\(sample\\)", join_fail_snippets, 5))
    if round_notes:
        sections.append(("📝 Catatan", round_notes, 5))

    lines = list(header)
    text = "\n".join(lines)
    truncated = False
    for title, items, max_items in sections:
        block = ["", f"**{title}:**"]
        for it in items[:max_items]:
            block.append(_short(str(it), 110))
        if len(items) > max_items:
            block.append(f"… \\+{len(items) - max_items} lainnya")
        candidate = text + "\n" + "\n".join(block)
        if len(candidate) > MSG_BUDGET:
            truncated = True
            break
        text = candidate
        lines = lines + block

    if truncated:
        text = text + "\n\n_… ringkasan dipotong agar muat di Telegram\\._"

    await _disconnect_pool()
    await _safe_edit(text, reply_markup=back_kb, parse_mode="Markdown")


async def manage_scrape_all_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_manage_scrape_all_data(context)
    if update.message:
        await update.message.reply_text("❌ Scrape grup \\(semua session\\) dibatalkan.")
    return ConversationHandler.END


async def manage_scrape_all_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_manage_scrape_all_data(context)
    await query.edit_message_text("❌ Scrape grup \\(semua session\\) dibatalkan\\.", parse_mode="Markdown")
    return ConversationHandler.END


def get_manage_scrape_all_conversation_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                manage_scrape_all_group_start, pattern=r"^manage_scrape_all_group$"
            ),
        ],
        states={
            MANAGE_SCRAPE_ALL_RANGE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, manage_scrape_all_receive_range
                ),
            ],
            MANAGE_SCRAPE_ALL_PER_SESSION_CAP: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    manage_scrape_all_receive_per_session_cap,
                ),
            ],
            MANAGE_SCRAPE_ALL_SOURCE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, manage_scrape_all_receive_source
                ),
            ],
            MANAGE_SCRAPE_ALL_TARGET: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, manage_scrape_all_receive_target
                ),
            ],
            MANAGE_SCRAPE_ALL_FILTER: [
                CallbackQueryHandler(
                    manage_scrape_all_pick_filter, pattern=r"^mscrape_f:(7|14|30)$"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", manage_scrape_all_cancel),
            CallbackQueryHandler(
                manage_scrape_all_cancel_cb, pattern=r"^cancel_manage_scrape_all$"
            ),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )


def _format_invite_flood_wait_seconds(sec: int) -> str:
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


def _scrape_truncate_btn(label: str, max_len: int = 42) -> str:
    label = (label or "").strip() or "Tanpa nama"
    if len(label) <= max_len:
        return label
    return label[: max_len - 1] + "…"


def _clear_scrape_conversation_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "scrape_group_phone",
        "scrape_group_candidates",
        "scrape_source_index",
        "scrape_src_page",
        "scrape_tgt_page",
    ):
        context.user_data.pop(key, None)


def _scrape_source_keyboard(candidates: list, page: int) -> InlineKeyboardMarkup:
    n = len(candidates)
    max_page = max(0, (n - 1) // SCRAPE_PAGE_SIZE) if n else 0
    page = max(0, min(page, max_page))
    start = page * SCRAPE_PAGE_SIZE
    chunk = candidates[start : start + SCRAPE_PAGE_SIZE]
    rows = []
    for j, c in enumerate(chunk):
        gi = start + j
        rows.append(
            [
                InlineKeyboardButton(
                    _scrape_truncate_btn(c.get("title", "")),
                    callback_data=f"sgs_i:{gi}",
                )
            ]
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"sgs_p:{page - 1}"))
    if start + SCRAPE_PAGE_SIZE < n:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"sgs_p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="cancel_scrape_group")])
    return InlineKeyboardMarkup(rows)


def _scrape_target_keyboard(candidates: list, page: int, exclude_index: int) -> InlineKeyboardMarkup:
    indexed = [(i, c) for i, c in enumerate(candidates) if i != exclude_index]
    n = len(indexed)
    max_page = max(0, (n - 1) // SCRAPE_PAGE_SIZE) if n else 0
    page = max(0, min(page, max_page))
    start = page * SCRAPE_PAGE_SIZE
    chunk = indexed[start : start + SCRAPE_PAGE_SIZE]
    rows = []
    for gi, c in chunk:
        rows.append(
            [
                InlineKeyboardButton(
                    _scrape_truncate_btn(c.get("title", "")),
                    callback_data=f"sgt_i:{gi}",
                )
            ]
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"sgt_p:{page - 1}"))
    if start + SCRAPE_PAGE_SIZE < n:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"sgt_p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="cancel_scrape_group")])
    return InlineKeyboardMarkup(rows)


async def _scrape_finish_edit_query(query, result, phone: str) -> None:
    """Tampilkan hasil scrape/undangan mengedit pesan callback saat ini."""
    back_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")],
            [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")],
        ]
    )

    if result is None:
        await query.edit_message_text(
            "❌ Terjadi kesalahan saat scrape/undangan\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")]],
            ),
            parse_mode="Markdown",
        )
        return

    src_gl = escape_markdown(str(result.get("source_group_label") or ""), version=1)
    tgt_gl = escape_markdown(str(result.get("group_label") or ""), version=1)

    if result.get("stopped_by_flood"):
        record_invite_flood(phone, int(result.get("flood_wait_seconds") or 0))
        sec = int(result.get("flood_wait_seconds") or 0)
        wait_human = escape_markdown(_format_invite_flood_wait_seconds(sec), version=1)
        inv = result.get("invited", 0)
        im = result.get("invited_mutual", 0)
        inm = result.get("invited_non_mutual", 0)
        mt = result.get("mutual_total", 0)
        nmt = result.get("non_mutual_total", 0)
        lines = [
            "⚠️ **Scrape/undangan dihentikan: limit Telegram \\(FloodWait panjang\\)**",
            "",
            f"⏳ Perkiraan tunggu: **~{wait_human}**",
            "",
            f"📱 Session: `{escape_markdown(phone, version=1)}`",
            f"📤 Sumber: `{src_gl}`",
            f"📥 Tujuan: `{tgt_gl}`",
            "",
            f"✅ Terundang sebelum limit: **{inv}**",
            f"   • Mutual: **{im}** / **{mt}**",
            f"   • Non\\-mutual: **{inm}** / **{nmt}**",
        ]
        samples = result.get("failed_sample") or []
        if samples:
            lines.append("")
            lines.append("Contoh error:")
            for uid, msg in samples[:5]:
                lines.append(f"• `{uid}`: {escape_markdown(msg, version=1)}")
        await query.edit_message_text("\n".join(lines), reply_markup=back_kb, parse_mode="Markdown")
        return

    if not result.get("ok"):
        err = escape_markdown(str(result.get("error") or "Gagal"), version=1)
        await query.edit_message_text(
            f"❌ **Scrape Grup gagal**\n\n{err}\n\n"
            f"📤 Sumber: `{src_gl}`\n"
            f"📥 Tujuan: `{tgt_gl}`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")]],
            ),
            parse_mode="Markdown",
        )
        return

    clear_invite_flood(phone)
    total = result.get("total_contacts", 0)
    inv = result.get("invited", 0)
    fail = result.get("failed", 0)
    samples = result.get("failed_sample") or []
    im = result.get("invited_mutual", 0)
    inm = result.get("invited_non_mutual", 0)
    mt = result.get("mutual_total", 0)
    nmt = result.get("non_mutual_total", 0)
    lines = [
        "✅ **Selesai: scrape → undang**",
        "",
        f"📱 Session: `{escape_markdown(phone, version=1)}`",
        f"📤 Grup sumber: `{src_gl}`",
        f"📥 Grup tujuan: `{tgt_gl}`",
        f"👥 Anggota terbaca & dijadwalkan undang: **{total}**",
        f"   • Mutual \\(buku kontak\\): **{mt}** → terundang **{im}**",
        f"   • Lainnya: **{nmt}** → terundang **{inm}**",
        f"✅ Total berhasil undang: **{inv}**",
        f"❌ Gagal / tidak terundang: **{fail}**",
    ]
    if samples:
        lines.append("")
        lines.append("Contoh error:")
        for uid, msg in samples[:5]:
            lines.append(f"• `{uid}`: {escape_markdown(msg, version=1)}")
    await query.edit_message_text("\n".join(lines), reply_markup=back_kb, parse_mode="Markdown")


async def scrape_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur scrape: muat daftar grup dari dialog session, tampilkan tombol pilih sumber."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.edit_message_text("❌ **Akses Ditolak**", parse_mode="Markdown")
        return ConversationHandler.END
    data = query.data or ""
    prefix = "scrape_group:"
    if not data.startswith(prefix):
        return ConversationHandler.END
    phone = data[len(prefix) :]
    if not phone:
        await query.edit_message_text("❌ Session tidak valid.")
        return ConversationHandler.END

    auth = TelethonAuth(phone)
    candidates = []
    try:
        if not auth.is_session_exists():
            await query.edit_message_text(
                "❌ File session tidak ditemukan.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")]],
                ),
            )
            return ConversationHandler.END
        if not await auth.connect():
            await query.edit_message_text(
                "❌ Session belum login\\. Gunakan `/login`\\.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")]],
                ),
            )
            return ConversationHandler.END
        candidates = await auth.list_joined_groups_for_scrape()
    finally:
        try:
            await auth.disconnect()
        except Exception:
            pass

    if not candidates:
        await query.edit_message_text(
            "❌ Tidak ada **grup** di dialog akun ini \\(supergroup/grup kecil; channel siaran tidak ditampilkan\\)\\.\n\n"
            "Pastikan session sudah bergabung ke setidaknya **dua** grup berbeda.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")]],
            ),
        )
        return ConversationHandler.END

    _clear_scrape_conversation_data(context)
    context.user_data["scrape_group_phone"] = phone
    context.user_data["scrape_group_candidates"] = candidates
    context.user_data["scrape_src_page"] = 0

    phone_esc = escape_markdown(phone, version=1)
    n = len(candidates)
    total_pages = max(1, (n + SCRAPE_PAGE_SIZE - 1) // SCRAPE_PAGE_SIZE)
    await query.edit_message_text(
        f"📥 **Scrape Grup** → undang ke grup lain\n\n"
        f"📱 Session: `{phone_esc}`\n"
        f"📤 **Langkah 1/2:** Pilih **grup sumber** \\(anggota diambil dari sini\\)\\.\n"
        f"Daftar dari **dialog Telegram** akun ini: **{n}** obrolan\\. Halaman **1/{total_pages}**\\.\n\n"
        f"/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=_scrape_source_keyboard(candidates, 0),
    )
    return SCRAPE_GROUP_PICK_SOURCE


async def scrape_group_source_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m = re.match(r"^sgs_p:(\d+)$", query.data or "")
    if not m:
        return SCRAPE_GROUP_PICK_SOURCE
    page = int(m.group(1))
    candidates = context.user_data.get("scrape_group_candidates")
    phone = context.user_data.get("scrape_group_phone")
    if not candidates or not phone:
        await query.edit_message_text("❌ Sesi pilihan berakhir\\. Ulangi dari menu session\\.", parse_mode="Markdown")
        return ConversationHandler.END
    max_page = max(0, (len(candidates) - 1) // SCRAPE_PAGE_SIZE)
    page = max(0, min(page, max_page))
    context.user_data["scrape_src_page"] = page
    phone_esc = escape_markdown(phone, version=1)
    n = len(candidates)
    total_pages = max(1, (n + SCRAPE_PAGE_SIZE - 1) // SCRAPE_PAGE_SIZE)
    human_page = page + 1
    await query.edit_message_text(
        f"📥 **Scrape Grup** → undang ke grup lain\n\n"
        f"📱 Session: `{phone_esc}`\n"
        f"📤 **Langkah 1/2:** Pilih **grup sumber**\\.\n"
        f"**{n}** obrolan\\. Halaman **{human_page}/{total_pages}**\\.\n\n"
        f"/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=_scrape_source_keyboard(candidates, page),
    )
    return SCRAPE_GROUP_PICK_SOURCE


async def scrape_group_pick_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m = re.match(r"^sgs_i:(\d+)$", query.data or "")
    if not m:
        return SCRAPE_GROUP_PICK_SOURCE
    idx = int(m.group(1))
    candidates = context.user_data.get("scrape_group_candidates")
    phone = context.user_data.get("scrape_group_phone")
    if not candidates or not phone or idx < 0 or idx >= len(candidates):
        await query.answer("Pilihan tidak valid.", show_alert=True)
        return SCRAPE_GROUP_PICK_SOURCE

    if len(candidates) < 2:
        await query.edit_message_text(
            "❌ Perlu **minimal 2** grup di dialog untuk memilih sumber dan tujuan berbeda.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Detail session", callback_data=f"session_info:{phone}")]],
            ),
        )
        _clear_scrape_conversation_data(context)
        return ConversationHandler.END

    context.user_data["scrape_source_index"] = idx
    context.user_data["scrape_tgt_page"] = 0
    sel = candidates[idx]
    title_esc = escape_markdown(sel.get("title", ""), version=1)
    phone_esc = escape_markdown(phone, version=1)
    n_other = len(candidates) - 1
    tgt_pages = max(1, (n_other + SCRAPE_PAGE_SIZE - 1) // SCRAPE_PAGE_SIZE)
    await query.edit_message_text(
        f"📥 **Scrape Grup**\n\n"
        f"📱 Session: `{phone_esc}`\n"
        f"📤 Sumber: `{title_esc}`\n\n"
        f"📥 **Langkah 2/2:** Pilih **grup tujuan** \\(anggota diundang ke sini\\)\\.\n"
        f"**{n_other}** pilihan\\. Halaman **1/{tgt_pages}**\\.\n\n"
        f"/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=_scrape_target_keyboard(candidates, 0, idx),
    )
    return SCRAPE_GROUP_PICK_TARGET


async def scrape_group_target_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m = re.match(r"^sgt_p:(\d+)$", query.data or "")
    if not m:
        return SCRAPE_GROUP_PICK_TARGET
    page = int(m.group(1))
    candidates = context.user_data.get("scrape_group_candidates")
    phone = context.user_data.get("scrape_group_phone")
    src_i = context.user_data.get("scrape_source_index")
    if candidates is None or phone is None or src_i is None:
        await query.edit_message_text("❌ Sesi pilihan berakhir\\. Ulangi dari menu session\\.", parse_mode="Markdown")
        return ConversationHandler.END
    n_other = len(candidates) - 1
    max_page = max(0, (n_other - 1) // SCRAPE_PAGE_SIZE) if n_other else 0
    page = max(0, min(page, max_page))
    context.user_data["scrape_tgt_page"] = page
    sel = candidates[src_i]
    title_esc = escape_markdown(sel.get("title", ""), version=1)
    phone_esc = escape_markdown(phone, version=1)
    tgt_pages = max(1, (n_other + SCRAPE_PAGE_SIZE - 1) // SCRAPE_PAGE_SIZE)
    human_page = page + 1
    await query.edit_message_text(
        f"📥 **Scrape Grup**\n\n"
        f"📱 Session: `{phone_esc}`\n"
        f"📤 Sumber: `{title_esc}`\n\n"
        f"📥 **Langkah 2/2:** Pilih **grup tujuan**\\.\n"
        f"**{n_other}** pilihan\\. Halaman **{human_page}/{tgt_pages}**\\.\n\n"
        f"/cancel untuk batal\\.",
        parse_mode="Markdown",
        reply_markup=_scrape_target_keyboard(candidates, page, src_i),
    )
    return SCRAPE_GROUP_PICK_TARGET


async def scrape_group_pick_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m = re.match(r"^sgt_i:(\d+)$", query.data or "")
    if not m:
        return SCRAPE_GROUP_PICK_TARGET
    idx = int(m.group(1))
    src_i = context.user_data.get("scrape_source_index")
    candidates = context.user_data.get("scrape_group_candidates")
    phone = context.user_data.get("scrape_group_phone")
    if src_i is None or not candidates or not phone or idx < 0 or idx >= len(candidates):
        await query.answer("Pilihan tidak valid.", show_alert=True)
        return SCRAPE_GROUP_PICK_TARGET
    if idx == src_i:
        await query.answer("Pilih grup tujuan yang berbeda dari sumber.", show_alert=True)
        return SCRAPE_GROUP_PICK_TARGET

    src_entry = candidates[src_i]
    tgt_entry = candidates[idx]
    await query.edit_message_text(
        "⏳ Membaca anggota grup sumber dan mengundang ke grup tujuan\\.\\.\\.",
        parse_mode="Markdown",
    )

    auth = TelethonAuth(phone)
    result = None
    try:
        if not auth.is_session_exists():
            result = {"ok": False, "error": "File session tidak ditemukan.", "source_group_label": "", "group_label": ""}
        elif not await auth.connect():
            result = {
                "ok": False,
                "error": "Session belum login. Gunakan /login.",
                "source_group_label": "",
                "group_label": "",
            }
        else:
            src_ent, err = await auth.resolve_scrape_peer_entry(src_entry)
            if err:
                result = {
                    "ok": False,
                    "error": f"Grup sumber: {err}",
                    "source_group_label": str(src_entry.get("title") or ""),
                    "group_label": str(tgt_entry.get("title") or ""),
                }
            else:
                tgt_ent, err2 = await auth.resolve_scrape_peer_entry(tgt_entry)
                if err2:
                    result = {
                        "ok": False,
                        "error": f"Grup tujuan: {err2}",
                        "source_group_label": str(src_entry.get("title") or ""),
                        "group_label": str(tgt_entry.get("title") or ""),
                    }
                else:
                    result = await auth.scrape_group_members_invite_entities(src_ent, tgt_ent)
    finally:
        try:
            await auth.disconnect()
        except Exception:
            pass

    _clear_scrape_conversation_data(context)
    await _scrape_finish_edit_query(query, result, phone)
    return ConversationHandler.END


async def scrape_group_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_scrape_conversation_data(context)
    if update.message:
        await update.message.reply_text("❌ Scrape grup dibatalkan.")
    return ConversationHandler.END


async def scrape_group_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_scrape_conversation_data(context)
    await query.edit_message_text("❌ Scrape grup dibatalkan.")
    return ConversationHandler.END


def get_scrape_group_conversation_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(scrape_group_start, pattern=r"^scrape_group:.+"),
        ],
        states={
            SCRAPE_GROUP_PICK_SOURCE: [
                CallbackQueryHandler(scrape_group_source_page, pattern=r"^sgs_p:\d+$"),
                CallbackQueryHandler(scrape_group_pick_source, pattern=r"^sgs_i:\d+$"),
            ],
            SCRAPE_GROUP_PICK_TARGET: [
                CallbackQueryHandler(scrape_group_target_page, pattern=r"^sgt_p:\d+$"),
                CallbackQueryHandler(scrape_group_pick_target, pattern=r"^sgt_i:\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", scrape_group_cancel),
            CallbackQueryHandler(scrape_group_cancel_cb, pattern=r"^cancel_scrape_group$"),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )


async def manage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu manajemen sessions"""
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
    
    keyboard = [
        [
            InlineKeyboardButton("📋 List Sessions", callback_data="list_sessions")
        ],
        [
            InlineKeyboardButton("✅ Check Sessions", callback_data="check_sessions")
        ],
        [
            InlineKeyboardButton("➕ Join Grup", callback_data="manage_join_all_group")
        ],
        [
            InlineKeyboardButton(
                "📥 Scrape Grup (semua)", callback_data="manage_scrape_all_group"
            )
        ],
        # [
        #     InlineKeyboardButton("🤖 Automation", callback_data="automation_menu")
        # ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh_menu")
        ],
        [
            InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🔧 **Menu Manajemen Sessions**\n\n"
        "Pilih opsi yang ingin Anda gunakan:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def manage_menu_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu manajemen dari callback (untuk edit message)"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await query.edit_message_text(
            "❌ **Akses Ditolak**\n\n"
            "Anda tidak memiliki izin untuk menggunakan bot ini.\n"
            "Silakan hubungi administrator untuk mendapatkan akses.",
            parse_mode='Markdown'
        )
        return
    
    keyboard = [
        [
            InlineKeyboardButton("📋 List Sessions", callback_data="list_sessions")
        ],
        [
            InlineKeyboardButton("✅ Check Sessions", callback_data="check_sessions")
        ],
        [
            InlineKeyboardButton("➕ Join Grup", callback_data="manage_join_all_group")
        ],
        [
            InlineKeyboardButton(
                "📥 Scrape Grup (semua)", callback_data="manage_scrape_all_group"
            )
        ],
        # [
        #     InlineKeyboardButton("🤖 Automation", callback_data="automation_menu")
        # ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh_menu")
        ],
        [
            InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🔧 **Menu Manajemen Sessions**\n\n"
        "Pilih opsi yang ingin Anda gunakan:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def list_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan daftar semua sessions"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Ambil session sesuai urutan kemunculan pertama (insertion order)
        # → konsisten dengan alur scrape (rentang `1-100`, dst.).
        session_files = _list_all_session_phones()

        if not session_files:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ **Tidak ada session ditemukan**\n\n"
                "Belum ada user yang berhasil login.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        # Pagination untuk handle banyak sessions (max 10 per halaman)
        PAGE_SIZE = 10
        total_pages = (len(session_files) + PAGE_SIZE - 1) // PAGE_SIZE
        
        # Ambil current_page dari context atau callback data
        current_page = 0
        if context.user_data and 'session_page' in context.user_data:
            current_page = int(context.user_data.get('session_page', 0))
        
        # Validasi page (pastikan tidak melebihi total pages)
        if current_page < 0:
            current_page = 0
        if current_page >= total_pages and total_pages > 0:
            current_page = total_pages - 1
        
        start_idx = current_page * PAGE_SIZE
        end_idx = min(start_idx + PAGE_SIZE, len(session_files))
        page_sessions = session_files[start_idx:end_idx]
        
        # Buat keyboard dengan daftar sessions (pagination)
        # Lebar total nomor urut disamakan agar tampilan rata.
        idx_width = len(str(len(session_files)))
        keyboard = []
        for offset, phone in enumerate(page_sessions):
            global_idx = start_idx + offset + 1
            mark_with_time = _list_mark_with_remaining(phone)
            label = phone if len(phone) <= 28 else (phone[:25] + "...")
            keyboard.append([InlineKeyboardButton(
                f"{global_idx:>{idx_width}}. {label}  {mark_with_time}",
                callback_data=f"session_info:{phone}"
            )])
        
        # Navigation buttons
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"list_sessions_page:{current_page - 1}"))
        if current_page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Selanjutnya ➡️", callback_data=f"list_sessions_page:{current_page + 1}"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        keyboard.append([
            InlineKeyboardButton("🔙 Kembali", callback_data="back_to_menu"),
            InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = f"📋 **Daftar Sessions**\n\n"
        text += f"Total: **{len(session_files)}** session(s)\n"
        if total_pages > 1:
            text += f"Halaman: **{current_page + 1}/{total_pages}**\n"
        text += f"Menampilkan: **{start_idx + 1}-{end_idx}** dari **{len(session_files)}**\n\n"
        text += (
            "**Keterangan tombol:** `1\\. <nomor>  ✅/❌`\n"
            "• **Kiri** \\= nomor urut global \\(sama dengan urutan di alur scrape\\)\n"
            "• **Kanan** ✅ \\= aman untuk undang • "
            "❌ `<sisa>` \\= masih FloodWait \\(`s`/`m`/`h`/`d`\\)\n\n"
        )
        text += "Pilih session untuk melihat detail \\(status diperbarui setiap kali halaman ini dibuka\\):"
        
        # Simpan current_page ke context untuk referensi berikutnya
        if context.user_data is None:
            context.user_data = {}
        context.user_data['session_page'] = current_page
        
        try:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as edit_error:
            # Handle error "Message is not modified" dengan graceful
            error_msg = str(edit_error).lower()
            if "not modified" in error_msg or "message is not modified" in error_msg:
                # Jika pesan tidak berubah, cukup answer callback saja
                # Ini terjadi ketika user menekan button yang sama dua kali
                pass
            else:
                # Jika error lain, raise kembali
                raise
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e), version=1)}", parse_mode='Markdown')


async def session_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan informasi session dan opsi download"""
    query = update.callback_query
    await query.answer()
    
    auth = None
    try:
        phone = query.data.split(":")[1]
        auth = TelethonAuth(phone)
        
        if not auth.is_session_exists():
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="list_sessions")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Session tidak ditemukan atau tidak valid.",
                reply_markup=reply_markup
            )
            return
        
        # Koneksi dan ambil info
        connected = await auth.connect()
        if not connected:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="list_sessions")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Tidak dapat terhubung ke session ini.",
                reply_markup=reply_markup
            )
            return
        
        info, error = await auth.get_session_info()
        
        if error:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="list_sessions")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"❌ {escape_markdown(error, version=1)}",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return

        contact_stats, _contact_err = await auth.get_contacts_mutual_stats()
        
        keyboard = [
            [
                # InlineKeyboardButton("📥 Download Session", callback_data=f"download:{phone}"),
                InlineKeyboardButton("📱 Read OTP", callback_data=f"read_otp:{phone}")
            ],
            # [
            #     InlineKeyboardButton("🔑 String Session", callback_data=f"string_session:{phone}")
            # ],
            [
                InlineKeyboardButton("👥 Invite Kontak", callback_data=f"invite_contacts:{phone}"),
                InlineKeyboardButton("📥 Scrape Grup", callback_data=f"scrape_group:{phone}"),
            ],
            [
                InlineKeyboardButton("➕ Gabung Grup", callback_data=f"join_session_group:{phone}"),
            ],
            [
                InlineKeyboardButton("🔙 Kembali", callback_data="list_sessions"),
                InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        name_esc = escape_markdown(info['name'] or 'Tidak ada', version=1)
        phone_esc = escape_markdown(info['phone'], version=1)
        path_esc = escape_markdown(info['session_path'], version=1)
        uname = info['username']
        if uname != 'Tidak ada':
            user_line = f"🔗 @{escape_markdown(uname, version=1)}"
        else:
            user_line = "🔗 —"

        text = (
            "📱 **Detail session**\n"
            "──────────────\n"
            "**Profil akun**\n"
            f"📞 `{phone_esc}`\n"
            f"👤 {name_esc}\n"
            f"{user_line}\n"
            f"🆔 `{info['id']}`\n"
        )
        if contact_stats:
            m = contact_stats["mutual"]
            nm = contact_stats["non_mutual"]
            tot = contact_stats["total"]
            text += (
                "──────────────\n"
                "**Buku kontak Telegram**\n"
                f"📊 Total **{tot}** _\\(tanpa bot\\)_\n"
                f"🤝 Mutual **{m}**\n"
                f"📭 Non\\-mutual **{nm}**\n"
            )
        else:
            text += (
                "──────────────\n"
                "**Buku kontak Telegram**\n"
                "_Tidak dapat dimuat\\. Coba buka lagi nanti\\._\n"
            )

        flood = get_invite_flood_status(phone)
        if flood.get("active"):
            rem = flood.get("remaining_sec", 0)
            fs = flood.get("flood_seconds", 0)
            rem_h = escape_markdown(human_duration_seconds(rem), version=1)
            text += (
                "──────────────\n"
                "**Limit undangan** ❌ _aktif_\n"
                f"⏱ Telegram minta **{fs}** d\n"
                f"⏳ Sisa estimasi **\\~{rem_h}** \\(**{rem}** d\\)\n"
            )
        elif flood.get("kind") == "expired":
            fs = int(flood.get("last_flood_seconds") or 0)
            text += (
                "──────────────\n"
                "**Limit undangan** ✅ _selesai_\n"
                f"Terakhir **{fs}** d \\(sudah lewat\\)\n"
            )
        else:
            text += (
                "──────────────\n"
                "**Limit undangan** ✅\n"
                "Belum ada catatan FloodWait dari invite kontak\\.\n"
            )
        
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e), version=1)}", parse_mode='Markdown')
    finally:
        # Pastikan koneksi selalu ditutup
        if auth:
            try:
                await auth.disconnect()
            except Exception:
                pass  # Ignore error saat disconnect


async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menghapus pesan bot"""
    query = update.callback_query
    if query:
        await query.answer("Pesan dihapus")
        try:
            await query.message.delete()
        except Exception as e:
            # Jika gagal menghapus (misalnya pesan sudah dihapus), cukup answer
            pass
    else:
        # Jika bukan dari callback query, tidak bisa menghapus
        pass


async def check_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengecek validitas semua sessions dan menghapus yang tidak valid"""
    query = update.callback_query
    if query:
        await query.answer()
    
    try:
        # Ambil semua file session
        session_files = []
        if os.path.exists(SESSION_DIR):
            for file in os.listdir(SESSION_DIR):
                if file.endswith('.session') and not file.endswith('-journal'):
                    phone = file.replace('.session', '')
                    session_files.append(phone)
        
        if not session_files:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="back_to_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            text = "❌ **Tidak ada session ditemukan**\n\nBelum ada user yang berhasil login."
            
            if query:
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
            return
        
        # Tampilkan progress awal
        progress_text = (
            f"⏳ **Mengecek Sessions\\.\\.\\.**\n\n"
            f"Total: **{len(session_files)}** session\\(s\\)\n"
            f"⚡ Mode: Concurrent ×10\n\n"
            f"\\[░░░░░░░░░░░░░░░░░░░░\\] 0%\n"
            f"📊 0 / {len(session_files)} session"
        )
        
        if query:
            progress_msg = await query.edit_message_text(progress_text, parse_mode='Markdown')
        else:
            progress_msg = await update.message.reply_text(progress_text, parse_mode='Markdown')
        
        # Proses pengecekan secara CONCURRENT (paralel) dengan semaphore
        # Semaphore 10 = max 10 koneksi bersamaan (aman untuk Telegram)
        valid_sessions = []
        invalid_sessions = []
        checked_count = [0]   # Mutable counter untuk progress
        valid_count = [0]
        invalid_count = [0]
        total = len(session_files)
        CHECK_SEMAPHORE = 10  # Max concurrent connections
        semaphore = asyncio.Semaphore(CHECK_SEMAPHORE)
        progress_done = [False]
        
        import time as _time
        start_time = _time.time()
        
        def _build_progress_bar(current, total_n):
            """Membuat progress bar visual"""
            pct = current / total_n if total_n > 0 else 0
            filled = int(pct * 20)
            bar = '█' * filled + '░' * (20 - filled)
            return bar, int(pct * 100)
        
        async def _update_progress():
            """Background task: update progress message setiap 3 detik"""
            last_count = -1
            while not progress_done[0]:
                await asyncio.sleep(3)
                if progress_done[0]:
                    break
                current = checked_count[0]
                if current == last_count:
                    continue  # Tidak ada perubahan, skip edit
                last_count = current
                
                bar, pct = _build_progress_bar(current, total)
                elapsed = _time.time() - start_time
                
                # Estimasi waktu tersisa
                if current > 0:
                    avg_per_session = elapsed / current
                    remaining = avg_per_session * (total - current)
                    remaining_min = int(remaining) // 60
                    remaining_sec = int(remaining) % 60
                    eta_str = f"{remaining_min}m {remaining_sec}s" if remaining_min > 0 else f"{remaining_sec}s"
                else:
                    eta_str = "menghitung\\.\\.\\."
                
                elapsed_min = int(elapsed) // 60
                elapsed_sec = int(elapsed) % 60
                elapsed_str = f"{elapsed_min}m {elapsed_sec}s" if elapsed_min > 0 else f"{elapsed_sec}s"
                
                try:
                    await progress_msg.edit_text(
                        f"⏳ **Mengecek Sessions\\.\\.\\.**\n\n"
                        f"\\[{bar}\\] **{pct}%**\n"
                        f"📊 {current} / {total} session\n"
                        f"✅ Valid: **{valid_count[0]}** ┃ ❌ Invalid: **{invalid_count[0]}**\n\n"
                        f"⏱️ Elapsed: {elapsed_str}\n"
                        f"⏳ ETA: ~{eta_str}",
                        parse_mode='Markdown'
                    )
                except Exception:
                    pass  # Ignore edit errors (rate limit, dll)
        
        async def check_one_session(phone):
            """Cek satu session dengan semaphore limiter"""
            async with semaphore:
                auth = None
                try:
                    auth = TelethonAuth(phone)
                    if not auth.is_session_exists():
                        invalid_count[0] += 1
                        return {'phone': phone, 'valid': False, 'reason': 'Session file tidak ditemukan'}
                    
                    # Cek validitas session
                    is_valid, reason = await auth.check_session_validity()
                    
                    if is_valid:
                        valid_count[0] += 1
                        return {'phone': phone, 'valid': True, 'reason': reason}
                    else:
                        invalid_count[0] += 1
                        # Hapus session yang tidak valid
                        try:
                            auth.cleanup_session()
                            _forget_session_order(phone)
                        except Exception as e:
                            print(f"Warning: Gagal menghapus session {phone}: {e}")
                        return {'phone': phone, 'valid': False, 'reason': reason}
                
                except Exception as e:
                    error_msg = str(e).lower()
                    invalid_count[0] += 1
                    # Jika flood/rate limit, tunggu sebelum release semaphore
                    if "flood" in error_msg or "rate limit" in error_msg:
                        await asyncio.sleep(5)
                    # Coba hapus session yang error
                    try:
                        if auth:
                            auth.cleanup_session()
                            _forget_session_order(phone)
                    except Exception:
                        pass
                    return {'phone': phone, 'valid': False, 'reason': f"Error: {str(e)}"}
                finally:
                    if auth:
                        try:
                            await auth.disconnect()
                        except Exception:
                            pass
                    checked_count[0] += 1
                    # Small delay setelah selesai agar tidak langsung rebut slot
                    await asyncio.sleep(0.3)
        
        # Jalankan pengecekan + progress updater secara bersamaan
        progress_task = asyncio.create_task(_update_progress())
        
        check_tasks = [check_one_session(phone) for phone in session_files]
        results = await asyncio.gather(*check_tasks, return_exceptions=True)
        
        # Stop progress updater
        progress_done[0] = True
        progress_task.cancel()
        try:
            await progress_task
        except (asyncio.CancelledError, Exception):
            pass
        
        elapsed = _time.time() - start_time
        
        # Proses hasil
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                invalid_sessions.append({
                    'phone': session_files[i],
                    'reason': f"Error: {str(result)}"
                })
            elif result['valid']:
                valid_sessions.append(result['phone'])
            else:
                invalid_sessions.append({
                    'phone': result['phone'],
                    'reason': result['reason']
                })
        
        # Tampilkan hasil
        elapsed_min = int(elapsed) // 60
        elapsed_sec = int(elapsed) % 60
        elapsed_str = f"{elapsed_min}m {elapsed_sec}s" if elapsed_min > 0 else f"{elapsed_sec}s"
        
        text = f"✅ **Pengecekan Sessions Selesai!**\n\n"
        text += f"📊 **Hasil:**\n"
        text += f"✅ Valid: **{len(valid_sessions)}** session(s)\n"
        text += f"❌ Tidak Valid: **{len(invalid_sessions)}** session(s)\n"
        text += f"⏱️ Waktu: **{elapsed_str}** \\(concurrent×{CHECK_SEMAPHORE}\\)\n\n"
        
        if valid_sessions:
            text += f"**✅ Sessions Valid:**\n"
            for phone in valid_sessions[:10]:  # Tampilkan max 10
                text += f"• `{escape_markdown(phone, version=1)}`\n"
            if len(valid_sessions) > 10:
                text += f"\\.\\.\\. dan {len(valid_sessions) - 10} session\\(s\\) lainnya\n"
            text += "\n"
        
        if invalid_sessions:
            text += f"**❌ Sessions Tidak Valid \\(Dihapus\\):**\n"
            for invalid in invalid_sessions[:10]:  # Tampilkan max 10
                text += f"• `{escape_markdown(invalid['phone'], version=1)}`\n"
                text += f"  └─ {escape_markdown(invalid['reason'], version=1)}\n"
            if len(invalid_sessions) > 10:
                text += f"\\.\\.\\. dan {len(invalid_sessions) - 10} session\\(s\\) lainnya\n"
        
        keyboard = [[
            InlineKeyboardButton("🔙 Kembali", callback_data="back_to_menu"),
            InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if query:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await progress_msg.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        
    except Exception as e:
        error_text = f"❌ **Error:** {escape_markdown(str(e), version=1)}"
        keyboard = [[
            InlineKeyboardButton("🔙 Kembali", callback_data="back_to_menu"),
            InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if query:
            await query.edit_message_text(error_text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(error_text, reply_markup=reply_markup, parse_mode='Markdown')


async def read_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membaca OTP terbaru dari Telegram dengan button refresh"""
    query = update.callback_query
    await query.answer()
    
    auth = None
    try:
        phone = query.data.split(":")[1]
        auth = TelethonAuth(phone)
        
        if not auth.is_session_exists():
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Session tidak ditemukan atau tidak valid.",
                reply_markup=reply_markup
            )
            return
        
        await query.edit_message_text("⏳ Membaca OTP terbaru...")
        
        # Baca OTP dengan limit 100
        otp_data, error = await auth.get_latest_otp(limit=100)
        
        if error:
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"read_otp:{phone}")],
                [InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"❌ {escape_markdown(error, version=1)}",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        if not otp_data:
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"read_otp:{phone}")],
                [InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Tidak ada OTP ditemukan.",
                reply_markup=reply_markup
            )
            return
        
        # Format pesan OTP
        text = "📱 **OTP Terbaru**\n\n"
        text += f"📅 **Waktu:** {otp_data['date'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        if otp_data['otp_codes']:
            text += "🔑 **Kode OTP ditemukan:**\n"
            for code in otp_data['otp_codes']:
                text += f"`{escape_markdown(code, version=1)}`\n"
            text += "\n"
        
        # Potong teks jika terlalu panjang
        message_text = otp_data['text']
        if len(message_text) > 500:
            message_text = message_text[:500] + "..."
        
        text += f"📄 **Pesan:**\n`{escape_markdown(message_text, version=1)}`"
        
        # Button refresh untuk mengambil OTP terbaru
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"read_otp:{phone}")],
            [
                InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}"),
                InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e), version=1)}", parse_mode='Markdown')
    finally:
        # Pastikan koneksi selalu ditutup
        if auth:
            try:
                await auth.disconnect()
            except Exception:
                pass  # Ignore error saat disconnect


async def download_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download session file"""
    query = update.callback_query
    await query.answer()
    
    try:
        phone = query.data.split(":")[1]
        auth = TelethonAuth(phone)
        
        if not auth.is_session_exists():
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="list_sessions")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Session tidak ditemukan atau tidak valid.",
                reply_markup=reply_markup
            )
            return
        
        # Kirim file string session (.session extension, isi text)
        if os.path.exists(auth.session_path):
            try:
                with open(auth.session_path, 'r', encoding='utf-8') as f:
                    string_session = f.read().strip()
                    # Kirim sebagai text message dengan format code
                    text = f"📥 **String Session File**\n\n"
                    text += f"📱 **Nomor:** `{phone}`\n\n"
                    text += f"```\n{string_session}\n```\n\n"
                    text += "⚠️ **PENTING:** Jangan share string session ini dengan siapapun!"
                    
                    await query.message.reply_text(
                        text,
                        parse_mode='Markdown'
                    )
            except UnicodeDecodeError:
                # Jika file binary (backward compatibility), kirim sebagai document
                with open(auth.session_path, 'rb') as f:
                    await query.message.reply_document(
                        document=f,
                        filename=f"{phone}.session",
                        caption=f"📥 **Session File (Binary)**\n\n📱 Nomor: `{phone}`\n\n⚠️ **PENTING:** Jangan share file ini dengan siapapun!",
                        parse_mode='Markdown'
                    )
            
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "✅ **Session file berhasil dikirim!**\n\n"
                "⚠️ File session mengandung kredensial login. Simpan dengan aman!",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="list_sessions")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ File session tidak ditemukan.",
                reply_markup=reply_markup
            )
            
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e), version=1)}", parse_mode='Markdown')


async def get_string_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mendapatkan string session"""
    query = update.callback_query
    await query.answer()
    
    auth = None
    try:
        phone = query.data.split(":")[1]
        auth = TelethonAuth(phone)
        
        if not auth.is_session_exists():
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "❌ Session tidak ditemukan atau tidak valid.",
                reply_markup=reply_markup
            )
            return
        
        await query.edit_message_text("⏳ Mengekspor string session...")
        
        # Export string session
        string_session, error = await auth.export_string_session()
        
        if error:
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"❌ {escape_markdown(error, version=1)}",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        # Kirim string session sebagai pesan code
        text = f"🔑 **String Session**\n\n"
        text += f"📱 **Nomor:** `{phone}`\n\n"
        text += f"```\n{string_session}\n```\n\n"
        text += "⚠️ **PENTING:**\n"
        text += "- Jangan share string session ini dengan siapapun!\n"
        text += "- String session ini bisa digunakan untuk login tanpa OTP\n"
        text += "- Simpan dengan aman dan jangan commit ke Git!"
        
        keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data=f"session_info:{phone}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {escape_markdown(str(e), version=1)}", parse_mode='Markdown')
    finally:
        # Pastikan koneksi selalu ditutup
        if auth:
            try:
                await auth.disconnect()
            except Exception:
                pass  # Ignore error saat disconnect


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk semua callback query dari manage menu"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        await query.answer("❌ Anda tidak memiliki izin untuk menggunakan bot ini.", show_alert=True)
        return
    
    # Hanya handle callback yang terkait dengan manage menu
    # Callback lain (seperti view_boosting) akan di-handle oleh handler lain
    if query.data == "list_sessions":
        await list_sessions(update, context)
    elif query.data.startswith("list_sessions_page:"):
        # Handle pagination
        try:
            page = int(query.data.split(":")[1])
            # Validasi page (tidak boleh negatif)
            if page < 0:
                page = 0
            # Simpan page ke context
            if context.user_data is None:
                context.user_data = {}
            context.user_data['session_page'] = page
            await list_sessions(update, context)
        except (ValueError, IndexError):
            # Jika format callback data tidak valid, reset ke halaman 1
            if context.user_data is None:
                context.user_data = {}
            context.user_data['session_page'] = 0
            await list_sessions(update, context)
    elif query.data == "check_sessions":
        await check_sessions(update, context)
    elif query.data == "automation_menu":
        await automation_menu(update, context)
    elif query.data == "back_to_menu":
        await manage_menu_from_callback(update, context)
    elif query.data == "refresh_menu":
        await manage_menu_from_callback(update, context)
    elif query.data.startswith("session_info:"):
        await session_info(update, context)
    elif query.data.startswith("read_otp:"):
        await read_otp(update, context)
    elif query.data.startswith("download:"):
        await download_session(update, context)
    elif query.data.startswith("string_session:"):
        await get_string_session(update, context)
    elif query.data == "delete_message":
        await delete_message(update, context)
    # Jika callback tidak dikenali, jangan lakukan apa-apa
    # Biarkan handler lain yang menanganinya


def get_manage_handlers():
    """Mengembalikan handlers untuk management"""
    # Note: CallbackQueryHandler tanpa pattern akan menangkap semua callback
    # Tapi karena automation handlers ditambahkan SEBELUM manage handlers,
    # callback yang match dengan automation pattern akan di-handle terlebih dahulu
    return [
        get_invite_contacts_conversation_handler(),
        get_join_session_group_conversation_handler(),
        get_manage_join_all_group_conversation_handler(),
        get_manage_scrape_all_conversation_handler(),
        get_scrape_group_conversation_handler(),
        CommandHandler("manage", manage_menu),
        CallbackQueryHandler(button_callback)  # Catch-all untuk manage menu callbacks
    ]

