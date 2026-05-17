"""Handlers untuk automation view boosting"""
import os
import re
import asyncio
import random
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler, filters, ContextTypes
from telethon_client import TelethonAuth
from config import SESSION_DIR
from utils.helpers import is_admin

# Konfigurasi delay untuk view boosting (dalam detik)
VIEW_DELAY_MIN = 3      # Minimum delay: 3 detik
VIEW_DELAY_MAX = 145    # Maximum delay: 50 detik

# Konfigurasi Auto View Boost
AUTO_CHECK_INTERVAL_MIN = 1    # Minimum interval cek (menit)
AUTO_CHECK_INTERVAL_MAX = 30   # Maximum interval cek (menit)
AUTO_CHECK_DEFAULT = 3         # Default interval cek (menit)

# Global state untuk auto view boost monitors
# Format: { chat_id: { 'channels': [...], 'per_channel': int, 'total_sessions': int,
#            'channel_session_pools': { channel: [sessions] }, 'channel_monitors': { channel: phone },
#            'interval': int, 'last_post_ids': { channel: post_id },
#            'task': asyncio.Task, 'active': bool, 'started_at': float } }
auto_monitors = {}

# Set untuk menyimpan active view boosting tasks (manual view boost)
# Agar bisa di-cancel saat shutdown
active_view_tasks = set()


def parse_channel_input(text: str):
    """
    Parse channel input yang bisa berupa:
    - Username: @channel_name atau channel_name
    - URL: https://t.me/channel_name atau https://t.me/channel_name/123
    
    Returns: (channel_username, post_id) - post_id bisa None jika tidak ada
    """
    text = text.strip()
    
    # Pattern untuk URL t.me dengan optional post ID
    # Format: https://t.me/channel_name/post_id atau https://t.me/channel_name
    url_pattern = r'(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)(?:/(\d+))?'
    
    match = re.match(url_pattern, text)
    if match:
        channel = match.group(1)
        post_id = int(match.group(2)) if match.group(2) else None
        return channel, post_id
    
    # Jika bukan URL, anggap sebagai username
    # Hapus @ jika ada
    if text.startswith('@'):
        text = text[1:]
    
    return text, None

# State untuk ConversationHandler
CHANNEL, SESSION_COUNT = range(2)

# State untuk Auto View Boost ConversationHandler
AUTO_CHANNEL, AUTO_SESSION_COUNT, AUTO_INTERVAL = range(10, 13)

# Dictionary untuk menyimpan automation state
automation_state = {}


async def automation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu automation"""
    query = update.callback_query
    if query:
        await query.answer()
    
    user_id = update.effective_user.id if query else update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        error_msg = "❌ **Akses Ditolak**\n\nAnda tidak memiliki izin untuk menggunakan bot ini.\nSilakan hubungi administrator untuk mendapatkan akses."
        if query:
            await query.edit_message_text(error_msg, parse_mode='Markdown')
        else:
            await update.message.reply_text(error_msg, parse_mode='Markdown')
        return
    
    chat_id = update.effective_chat.id
    monitor = auto_monitors.get(chat_id)
    monitor_active = monitor and monitor.get('active', False)
    
    keyboard = [
        [
            InlineKeyboardButton("👁️ View Boosting", callback_data="view_boosting")
        ],
        [
            InlineKeyboardButton(
                "🔴 Stop Auto View" if monitor_active else "🟢 Auto View Boost",
                callback_data="stop_auto_view" if monitor_active else "auto_view_start"
            )
        ],
        [
            InlineKeyboardButton("🔙 Kembali", callback_data="back_to_menu")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "🤖 **Menu Automation**\n\n"
    text += "Pilih fitur automation yang ingin digunakan:\n\n"
    
    if monitor_active:
        channels_str = ', '.join(monitor.get('channels', []))
        interval = monitor.get('interval', AUTO_CHECK_DEFAULT)
        started = monitor.get('started_at', 0)
        elapsed = int(time.time() - started) if started else 0
        elapsed_min = elapsed // 60
        elapsed_sec = elapsed % 60
        text += f"📡 **Auto View Boost Aktif**\n"
        text += f"📺 Channel: `{escape_markdown(channels_str, version=1)}`\n"
        text += f"⏱️ Interval: {interval} menit\n"
        text += f"👥 Sessions: {monitor.get('session_count', 0)} akun\n"
        text += f"⏳ Berjalan: {elapsed_min}m {elapsed_sec}s\n"
    
    if query:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )


async def view_boosting_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai proses view boosting"""
    query = update.callback_query
    if query:
        await query.answer()
    
    user_id = update.effective_user.id
    
    # Cek apakah user adalah admin
    if not is_admin(user_id):
        error_msg = "❌ **Akses Ditolak**\n\nAnda tidak memiliki izin untuk menggunakan bot ini.\nSilakan hubungi administrator untuk mendapatkan akses."
        if query:
            await query.edit_message_text(error_msg, parse_mode='Markdown')
        else:
            await update.message.reply_text(error_msg, parse_mode='Markdown')
        return ConversationHandler.END
    
    # Cek jumlah sessions yang tersedia
    session_files = []
    if os.path.exists(SESSION_DIR):
        for file in os.listdir(SESSION_DIR):
            if file.endswith('.session') and not file.endswith('-journal'):
                phone = file.replace('.session', '')
                session_files.append(phone)
    
    if not session_files:
        keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="automation_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "❌ **Tidak ada session ditemukan**\n\n"
            "Belum ada user yang berhasil login.\n"
            "Silakan login terlebih dahulu dengan /login",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_automation")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"👁️ **View Boosting**\n\n"
        f"📊 **Sessions tersedia:** {len(session_files)} akun\n\n"
        f"**Input Channel**\n\n"
        f"Kirim username channel atau URL postingan yang ingin di\\-boost views\\-nya\\.\n\n"
        f"**Format yang Didukung:**\n"
        f"• Username: `@channel_name` atau `channel_name`\n"
        f"• URL channel: `https://t.me/channel_name`\n"
        f"• URL post spesifik: `https://t.me/channel_name/123`\n\n"
        f"**Multiple Channel \\(dipisah koma atau baris baru\\):**\n"
        f"`@channel1, https://t.me/channel2/456`\n\n"
        f"**📌 Catatan Penting:**\n"
        f"• Jika URL post diberikan, akan boost post spesifik tersebut\n"
        f"• Jika hanya channel, akan boost post terbaru\n"
        f"• Userbot akan otomatis join channel jika diperlukan\n\n"
        f"Gunakan /cancel untuk membatalkan\\.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return CHANNEL


async def receive_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima channel username (bisa beberapa channel sekaligus, bisa URL atau username)"""
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
    
    channels_input = update.message.text.strip()
    
    if not channels_input:
        await update.message.reply_text(
            "❌ Channel tidak valid!\n\n"
            "Silakan kirim username channel atau URL yang valid.\n"
            "Contoh:\n"
            "• @channel_name\n"
            "• channel_name\n"
            "• https://t.me/channel_name\n"
            "• https://t.me/channel_name/123"
        )
        return CHANNEL
    
    # Parse multiple channels (bisa dipisah koma atau baris baru)
    # Sekarang support URL t.me dan extract post ID jika ada
    channels = []
    channel_post_ids = {}  # Menyimpan post_id spesifik per channel
    
    for line in channels_input.split('\n'):
        for ch in line.split(','):
            ch = ch.strip()
            if ch:
                # Parse channel (bisa URL atau username)
                channel_name, post_id = parse_channel_input(ch)
                if channel_name:
                    channels.append(channel_name)
                    # Simpan post_id jika ada dari URL
                    if post_id:
                        channel_post_ids[channel_name.lower()] = post_id
    
    # Hapus duplikat sambil menjaga urutan
    seen = set()
    unique_channels = []
    for ch in channels:
        if ch.lower() not in seen:
            seen.add(ch.lower())
            unique_channels.append(ch)
    
    if not unique_channels:
        await update.message.reply_text(
            "❌ Tidak ada channel valid yang ditemukan!\n\n"
            "Silakan kirim username channel atau URL yang valid.\n"
            "Contoh:\n"
            "• @channel_name\n"
            "• https://t.me/channel_name/123"
        )
        return CHANNEL
    
    # Simpan channels dan post_ids ke context
    context.user_data['channels'] = unique_channels
    context.user_data['channel_post_ids'] = channel_post_ids  # Post ID spesifik dari URL
    
    # Log untuk debug
    print(f"[VIEW BOOST] Parsed channels: {unique_channels}")
    print(f"[VIEW BOOST] Parsed post IDs: {channel_post_ids}")
    
    # Ambil semua sessions
    session_files = []
    if os.path.exists(SESSION_DIR):
        for file in os.listdir(SESSION_DIR):
            if file.endswith('.session') and not file.endswith('-journal'):
                phone = file.replace('.session', '')
                session_files.append(phone)
    
    if not session_files:
        await update.message.reply_text(
            "❌ Tidak ada session ditemukan."
        )
        return ConversationHandler.END
    
    # Simpan session_files ke context
    context.user_data['session_files'] = session_files
    total_sessions = len(session_files)
    
    # Tampilkan konfirmasi channel dan pilihan jumlah sesi
    channels_text = '\n'.join([f"• `{escape_markdown(ch, version=1)}`" for ch in unique_channels])
    
    # Buat inline buttons untuk pilihan jumlah sesi
    keyboard = []
    
    # Opsi jumlah sesi berdasarkan total yang tersedia
    session_options = []
    if total_sessions >= 10:
        session_options.append(10)
    if total_sessions >= 25:
        session_options.append(25)
    if total_sessions >= 50:
        session_options.append(50)
    if total_sessions >= 100:
        session_options.append(100)
    
    # Buat baris buttons untuk jumlah preset
    row = []
    for opt in session_options:
        row.append(InlineKeyboardButton(f"📱 {opt} Akun", callback_data=f"session_count:{opt}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    # Tombol "Semua" dan "Custom"
    keyboard.append([
        InlineKeyboardButton(f"📱 Semua ({total_sessions})", callback_data=f"session_count:all"),
        InlineKeyboardButton("✏️ Custom", callback_data="session_count:custom")
    ])
    
    # Tombol batal
    keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="cancel_automation")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Tampilkan info post ID jika ada dari URL
    post_info = ""
    if channel_post_ids:
        post_info = "\n📌 **Post ID dari URL:**\n"
        for ch, pid in channel_post_ids.items():
            post_info += f"• {escape_markdown(ch, version=1)}: Post \\#{pid}\n"
        post_info += "\n"
    
    await update.message.reply_text(
        f"✅ **Channel diterima:**\n{channels_text}\n{post_info}"
        f"📊 **Sessions tersedia:** {total_sessions} akun\n\n"
        f"**Pilih jumlah sesi yang akan digunakan:**\n\n"
        f"Semakin banyak sesi, semakin banyak views yang ditambahkan\\.\n"
        f"Atau ketik angka secara langsung \\(contoh: `15`\\)\\.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return SESSION_COUNT


async def receive_session_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima pilihan jumlah sesi dari callback button"""
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
        return ConversationHandler.END
    
    # Parse callback data
    data = query.data.replace("session_count:", "")
    
    if data == "custom":
        # Minta input manual
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_automation")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        total_sessions = len(context.user_data.get('session_files', []))
        
        await query.edit_message_text(
            f"✏️ **Custom Session Count**\n\n"
            f"📊 Sessions tersedia: **{total_sessions}** akun\n\n"
            f"Ketik jumlah sesi yang ingin digunakan \\(1\\-{total_sessions}\\)\\.\n"
            f"Contoh: `15`",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return SESSION_COUNT
    
    # Tentukan jumlah sesi
    session_files = context.user_data.get('session_files', [])
    
    if data == "all":
        session_count = len(session_files)
    else:
        try:
            session_count = int(data)
        except ValueError:
            session_count = len(session_files)
    
    # Batasi jumlah sesi
    session_count = min(session_count, len(session_files))
    session_count = max(1, session_count)
    
    # Proses view boosting
    await start_view_boosting(query, context, session_count)
    return ConversationHandler.END


async def receive_session_count_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima jumlah sesi dari input text"""
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
    
    text = update.message.text.strip()
    session_files = context.user_data.get('session_files', [])
    total_sessions = len(session_files)
    
    # Validasi input
    try:
        session_count = int(text)
        if session_count < 1:
            raise ValueError("Must be positive")
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_automation")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"❌ Input tidak valid\\!\n\n"
            f"Silakan masukkan angka antara 1\\-{total_sessions}\\.\n"
            f"Contoh: `15`",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return SESSION_COUNT
    
    # Batasi jumlah sesi
    session_count = min(session_count, total_sessions)
    
    # Proses view boosting
    await start_view_boosting_from_message(update, context, session_count)
    return ConversationHandler.END


async def start_view_boosting(query, context: ContextTypes.DEFAULT_TYPE, session_count: int):
    """Memulai proses view boosting dari callback"""
    unique_channels = context.user_data.get('channels', [])
    session_files = context.user_data.get('session_files', [])
    channel_post_ids = context.user_data.get('channel_post_ids', {})  # Post ID dari URL
    
    # Ambil hanya sejumlah sesi yang dipilih
    selected_sessions = session_files[:session_count]
    
    # Update pesan dengan status processing
    channels_text = ', '.join([f"`{escape_markdown(ch, version=1)}`" for ch in unique_channels])
    await query.edit_message_text(
        f"⏳ **Memulai View Boosting\\.\\.\\.**\n\n"
        f"📱 Channel: **{len(unique_channels)}** channel\\(s\\)\n"
        f"👥 Sessions: **{len(selected_sessions)}** dari {len(session_files)} akun\n\n"
        f"📺 {channels_text}\n\n"
        f"Bot akan otomatis mendeteksi postingan terbaru dari setiap channel\\.\n"
        f"Mohon tunggu, sedang memproses\\.\\.\\.",
        parse_mode='Markdown'
    )
    
    # Proses view boosting dengan post_ids dari URL
    try:
        all_results = await process_view_boosting_multiple_channels(selected_sessions, unique_channels, channel_post_ids)
    except Exception as e:
        error_msg = str(e).lower()
        if "rate limit" in error_msg or "flood" in error_msg:
            await query.edit_message_text(
                "❌ **Error:** Rate limit dari Telegram\n\n"
                "Terlalu banyak request dalam waktu singkat\\.\n"
                "Silakan tunggu beberapa menit dan coba lagi\\.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ **Error saat memproses view boosting:**\n\n`{escape_markdown(str(e), version=1)}`\n\n"
                "Silakan coba lagi atau hubungi admin jika masalah berlanjut\\.",
                parse_mode='Markdown'
            )
        context.user_data.clear()
        return
    
    # Tampilkan hasil
    await display_results(query, context, all_results, unique_channels, selected_sessions, session_files)


async def start_view_boosting_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE, session_count: int):
    """Memulai proses view boosting dari text message"""
    unique_channels = context.user_data.get('channels', [])
    session_files = context.user_data.get('session_files', [])
    channel_post_ids = context.user_data.get('channel_post_ids', {})  # Post ID dari URL
    
    # Ambil hanya sejumlah sesi yang dipilih
    selected_sessions = session_files[:session_count]
    
    # Kirim pesan status processing
    channels_text = ', '.join([f"`{escape_markdown(ch, version=1)}`" for ch in unique_channels])
    status_msg = await update.message.reply_text(
        f"⏳ **Memulai View Boosting\\.\\.\\.**\n\n"
        f"📱 Channel: **{len(unique_channels)}** channel\\(s\\)\n"
        f"👥 Sessions: **{len(selected_sessions)}** dari {len(session_files)} akun\n\n"
        f"📺 {channels_text}\n\n"
        f"Bot akan otomatis mendeteksi postingan terbaru dari setiap channel\\.\n"
        f"Mohon tunggu, sedang memproses\\.\\.\\.",
        parse_mode='Markdown'
    )
    
    # Proses view boosting dengan post_ids dari URL
    try:
        all_results = await process_view_boosting_multiple_channels(selected_sessions, unique_channels, channel_post_ids)
    except Exception as e:
        error_msg = str(e).lower()
        if "rate limit" in error_msg or "flood" in error_msg:
            await status_msg.edit_text(
                "❌ **Error:** Rate limit dari Telegram\n\n"
                "Terlalu banyak request dalam waktu singkat\\.\n"
                "Silakan tunggu beberapa menit dan coba lagi\\.",
                parse_mode='Markdown'
            )
        else:
            await status_msg.edit_text(
                f"❌ **Error saat memproses view boosting:**\n\n`{escape_markdown(str(e), version=1)}`\n\n"
                "Silakan coba lagi atau hubungi admin jika masalah berlanjut\\.",
                parse_mode='Markdown'
            )
        context.user_data.clear()
        return
    
    # Tampilkan hasil
    await display_results_message(status_msg, context, all_results, unique_channels, selected_sessions, session_files)


async def display_results(query, context, all_results, unique_channels, selected_sessions, session_files):
    """Menampilkan hasil view boosting dari callback"""
    text = f"✅ **View Boosting Selesai\\!**\n\n"
    text += f"📱 **Channel:** {len(unique_channels)} channel\\(s\\)\n"
    text += f"👥 **Sessions:** {len(selected_sessions)} dari {len(session_files)} akun\n\n"
    
    total_success = 0
    total_failed = 0
    
    for channel_result in all_results:
        channel = channel_result['channel']
        post_id = channel_result.get('post_id')
        results = channel_result['results']
        success_count = sum(1 for r in results if r['success'])
        failed_count = len(results) - success_count
        total_success += success_count
        total_failed += failed_count
        
        escaped_channel = escape_markdown(channel, version=1)
        text += f"📺 **{escaped_channel}**\n"
        if post_id:
            channel_username = channel.replace('@', '') if channel.startswith('@') else channel
            post_link = f"https://t.me/{channel_username}/{post_id}"
            text += f"   🔗 [📎 Link Postingan]({post_link})\n"
        text += f"   ✅ Berhasil: {success_count} akun\n"
        text += f"   ❌ Gagal: {failed_count} akun\n"
        text += f"   📈 Views: {success_count}\n\n"
    
    text += f"📊 **Total:**\n"
    text += f"✅ Berhasil: **{total_success}** views\n"
    text += f"❌ Gagal: **{total_failed}** akun\n"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Menu Automation", callback_data="automation_menu")],
        [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    context.user_data.clear()


async def display_results_message(message, context, all_results, unique_channels, selected_sessions, session_files):
    """Menampilkan hasil view boosting dari message"""
    text = f"✅ **View Boosting Selesai\\!**\n\n"
    text += f"📱 **Channel:** {len(unique_channels)} channel\\(s\\)\n"
    text += f"👥 **Sessions:** {len(selected_sessions)} dari {len(session_files)} akun\n\n"
    
    total_success = 0
    total_failed = 0
    
    for channel_result in all_results:
        channel = channel_result['channel']
        post_id = channel_result.get('post_id')
        results = channel_result['results']
        success_count = sum(1 for r in results if r['success'])
        failed_count = len(results) - success_count
        total_success += success_count
        total_failed += failed_count
        
        escaped_channel = escape_markdown(channel, version=1)
        text += f"📺 **{escaped_channel}**\n"
        if post_id:
            channel_username = channel.replace('@', '') if channel.startswith('@') else channel
            post_link = f"https://t.me/{channel_username}/{post_id}"
            text += f"   🔗 [📎 Link Postingan]({post_link})\n"
        text += f"   ✅ Berhasil: {success_count} akun\n"
        text += f"   ❌ Gagal: {failed_count} akun\n"
        text += f"   📈 Views: {success_count}\n\n"
    
    text += f"📊 **Total:**\n"
    text += f"✅ Berhasil: **{total_success}** views\n"
    text += f"❌ Gagal: **{total_failed}** akun\n"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Menu Automation", callback_data="automation_menu")],
        [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    context.user_data.clear()




async def process_view_boosting_multiple_channels(session_files, channels, channel_post_ids=None):
    """Memproses view boosting untuk multiple channels dan semua sessions
    
    Args:
        session_files: List of phone numbers (session files)
        channels: List of channel usernames
        channel_post_ids: Dict mapping channel name (lowercase) to specific post ID from URL
    """
    if channel_post_ids is None:
        channel_post_ids = {}
    
    all_results = []
    
    # Proses setiap channel dengan error handling
    for channel in channels:
        try:
            # Cek apakah ada post_id spesifik dari URL untuk channel ini
            specific_post_id = channel_post_ids.get(channel.lower())
            
            if specific_post_id:
                print(f"[VIEW BOOST] Using specific post ID {specific_post_id} for {channel}")
            
            results, post_id = await process_view_boosting(session_files, channel, specific_post_id)
            all_results.append({
                'channel': channel,
                'post_id': post_id,
                'results': results
            })
        except Exception as e:
            # Jika error saat memproses channel, simpan error tapi lanjutkan channel berikutnya
            error_msg = str(e).lower()
            all_results.append({
                'channel': channel,
                'post_id': None,
                'results': [{
                    'phone': 'ALL',
                    'success': False,
                    'error': f'Error memproses channel: {str(e)}'
                }],
                'error': str(e)
            })
            # Log error tapi lanjutkan ke channel berikutnya
            print(f"Error processing channel {channel}: {str(e)}")
    
    return all_results


async def process_view_boosting(session_files, channel, post_id=None):
    """Memproses view boosting untuk semua sessions pada satu channel
    
    Args:
        session_files: List of phone numbers
        channel: Channel username (tanpa @)
        post_id: Specific post ID from URL (optional, if None will get latest)
    """
    results = []
    actual_post_id = post_id  # Gunakan post_id dari parameter jika ada
    
    print(f"[VIEW BOOST] Starting for channel: {channel}, sessions: {len(session_files)}, given_post_id: {post_id}")
    
    # Ambil post_id terbaru HANYA jika tidak diberikan
    if actual_post_id is None:
        # Gunakan session pertama untuk mendapatkan post terbaru
        if session_files:
            auth = None
            max_attempts = min(5, len(session_files))  # Coba lebih banyak session
            for attempt in range(max_attempts):
                try:
                    auth = TelethonAuth(session_files[attempt])
                    if auth.is_session_exists():
                        connected = await auth.connect()
                        print(f"[VIEW BOOST] Session {session_files[attempt]} connected: {connected}")
                        if connected:
                            latest_post, error = await auth.get_latest_post(channel)
                            if latest_post:
                                actual_post_id = latest_post.id
                                print(f"[VIEW BOOST] Got latest post ID: {actual_post_id}")
                                await auth.disconnect()
                                break  # Berhasil, keluar dari loop
                            elif error:
                                print(f"[VIEW BOOST] Error getting post: {error}")
                                if "rate limit" not in error.lower():
                                    # Jika bukan rate limit, coba session berikutnya
                                    await auth.disconnect()
                                    await asyncio.sleep(1)
                                    continue
                        await auth.disconnect()
                except Exception as e:
                    # Log error tapi coba session berikutnya
                    print(f"[VIEW BOOST] Exception for session {session_files[attempt]}: {str(e)}")
                    error_msg = str(e).lower()
                    if "rate limit" in error_msg or "flood" in error_msg:
                        # Jika rate limit, tunggu sebentar sebelum coba lagi
                        await asyncio.sleep(3)
                    continue
                finally:
                    if auth:
                        try:
                            await auth.disconnect()
                        except Exception:
                            pass
                    auth = None
                    await asyncio.sleep(0.5)  # Small delay between attempts
    else:
        print(f"[VIEW BOOST] Using provided post ID: {actual_post_id}")
    
    print(f"[VIEW BOOST] Final post ID: {actual_post_id}")
    
    # Process dengan semaphore untuk limit concurrent connections
    # Limit 5 concurrent untuk menghindari rate limit Telegram
    semaphore = asyncio.Semaphore(5)
    
    async def view_with_session(phone, session_index):
        async with semaphore:
            auth = None
            try:
                # Random delay sebelum view (3 detik - 2 menit)
                # Delay berbeda untuk setiap akun agar terlihat natural
                delay = random.uniform(VIEW_DELAY_MIN, VIEW_DELAY_MAX)
                print(f"[VIEW BOOST] Session {phone} waiting {delay:.1f}s before view...")
                await asyncio.sleep(delay)
                
                # Validasi session file
                auth = TelethonAuth(phone)
                if not auth.is_session_exists():
                    print(f"[VIEW BOOST] Session not found: {phone}")
                    return {
                        'phone': phone,
                        'success': False,
                        'error': 'Session tidak ditemukan'
                    }
                
                # Cek apakah post_id valid
                if actual_post_id is None:
                    print(f"[VIEW BOOST] No post ID for: {phone}")
                    return {
                        'phone': phone,
                        'success': False,
                        'error': 'Post ID tidak ditemukan'
                    }
                
                # Coba view post dengan retry mechanism
                max_retries = 2
                last_error = None
                
                for retry in range(max_retries):
                    try:
                        # view_post akan menggunakan actual_post_id yang sudah didapatkan
                        print(f"[VIEW BOOST] Viewing post {actual_post_id} with {phone} (attempt {retry+1})")
                        success, message = await auth.view_post(channel, actual_post_id)
                        
                        if success:
                            print(f"[VIEW BOOST] SUCCESS: {phone} -> {message}")
                            return {
                                'phone': phone,
                                'success': True,
                                'error': None
                            }
                        else:
                            # Jika gagal, simpan error untuk retry
                            print(f"[VIEW BOOST] FAILED: {phone} -> {message}")
                            last_error = message
                            error_lower = message.lower() if message else ""
                            
                            # Jika rate limit, tunggu lebih lama sebelum retry
                            if "rate limit" in error_lower or "flood" in error_lower:
                                if retry < max_retries - 1:
                                    await asyncio.sleep(3)  # Tunggu 3 detik sebelum retry
                                    continue
                            
                            # Jika unauthorized atau error permanen, langsung return
                            if "unauthorized" in error_lower or "tidak valid" in error_lower:
                                return {
                                    'phone': phone,
                                    'success': False,
                                    'error': message
                                }
                            
                            # Retry untuk error lainnya
                            if retry < max_retries - 1:
                                await asyncio.sleep(1)
                                continue
                            
                            # Jika semua retry gagal, return error
                            return {
                                'phone': phone,
                                'success': False,
                                'error': message
                            }
                            
                    except Exception as e:
                        error_msg = str(e).lower()
                        last_error = str(e)
                        
                        # Handle specific errors
                        if "rate limit" in error_msg or "flood" in error_msg:
                            if retry < max_retries - 1:
                                await asyncio.sleep(3)
                                continue
                            return {
                                'phone': phone,
                                'success': False,
                                'error': 'Rate limit: Terlalu banyak request'
                            }
                        elif "unauthorized" in error_msg or "auth" in error_msg:
                            return {
                                'phone': phone,
                                'success': False,
                                'error': 'Session tidak valid (unauthorized)'
                            }
                        elif "timeout" in error_msg or "connection" in error_msg:
                            if retry < max_retries - 1:
                                await asyncio.sleep(2)
                                continue
                            return {
                                'phone': phone,
                                'success': False,
                                'error': 'Timeout: Gagal terhubung'
                            }
                        else:
                            # Error lainnya, retry sekali
                            if retry < max_retries - 1:
                                await asyncio.sleep(1)
                                continue
                            return {
                                'phone': phone,
                                'success': False,
                                'error': str(e)
                            }
                
                # Jika semua retry gagal
                return {
                    'phone': phone,
                    'success': False,
                    'error': last_error or 'Gagal view post'
                }
                
            except Exception as e:
                # Catch-all untuk error yang tidak terduga
                error_msg = str(e).lower()
                if "rate limit" in error_msg:
                    return {
                        'phone': phone,
                        'success': False,
                        'error': 'Rate limit: Terlalu banyak request'
                    }
                else:
                    return {
                        'phone': phone,
                        'success': False,
                        'error': f'Error tidak terduga: {str(e)}'
                    }
            finally:
                # Pastikan disconnect dengan error handling
                if auth:
                    try:
                        await auth.disconnect()
                    except Exception as disconnect_error:
                        # Ignore disconnect errors
                        pass
    
    # Jalankan semua tasks secara concurrent
    # Random delay sudah ditangani di dalam view_with_session
    print(f"[VIEW BOOST] Starting {len(session_files)} sessions with random delay {VIEW_DELAY_MIN}-{VIEW_DELAY_MAX}s each")
    tasks = []
    for i, phone in enumerate(session_files):
        task = asyncio.ensure_future(view_with_session(phone, i))
        tasks.append(task)
        active_view_tasks.add(task)
        task.add_done_callback(active_view_tasks.discard)
    
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        # Jika di-cancel saat shutdown, cancel semua child tasks
        for t in tasks:
            if not t.done():
                t.cancel()
        raise
    
    # Handle exceptions
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                'phone': session_files[i],
                'success': False,
                'error': str(result)
            })
        else:
            processed_results.append(result)
    
    return processed_results, actual_post_id


# =============================================
# AUTO VIEW BOOST - Monitor & Auto Boost
# =============================================

async def auto_view_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Memulai setup Auto View Boost"""
    query = update.callback_query
    if query:
        await query.answer()
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if not is_admin(user_id):
        if query:
            await query.edit_message_text(
                "❌ **Akses Ditolak**\n\nAnda tidak memiliki izin\\.",
                parse_mode='Markdown'
            )
        return ConversationHandler.END
    
    # Cek apakah sudah ada monitor aktif
    monitor = auto_monitors.get(chat_id)
    if monitor and monitor.get('active', False):
        keyboard = [
            [InlineKeyboardButton("🔴 Stop Auto View", callback_data="stop_auto_view")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="automation_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        channels_str = ', '.join(monitor.get('channels', []))
        await query.edit_message_text(
            f"⚠️ **Auto View Boost sudah aktif\\!**\n\n"
            f"📺 Channel: `{escape_markdown(channels_str, version=1)}`\n"
            f"⏱️ Interval: {monitor.get('interval', AUTO_CHECK_DEFAULT)} menit\n"
            f"👥 Sessions: {monitor.get('session_count', 0)} akun\n\n"
            f"Stop monitor yang aktif terlebih dahulu sebelum membuat yang baru\\.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Cek jumlah sessions yang tersedia
    session_files = []
    if os.path.exists(SESSION_DIR):
        for file in os.listdir(SESSION_DIR):
            if file.endswith('.session') and not file.endswith('-journal'):
                phone = file.replace('.session', '')
                session_files.append(phone)
    
    if not session_files:
        keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="automation_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "❌ **Tidak ada session ditemukan**\n\n"
            "Belum ada user yang berhasil login\\.\n"
            "Silakan login terlebih dahulu dengan /login",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🟢 **Auto View Boost**\n\n"
        f"📊 **Sessions tersedia:** {len(session_files)} akun\n\n"
        f"Fitur ini akan memonitor channel dan otomatis boost view\n"
        f"setiap ada postingan baru\\.\n\n"
        f"**Kirim username channel yang ingin dimonitor:**\n\n"
        f"**Format yang Didukung:**\n"
        f"• Username: `@channel_name` atau `channel_name`\n"
        f"• URL: `https://t.me/channel_name`\n"
        f"• Multiple channel \\(pisah koma\\): `@ch1, @ch2`\n\n"
        f"Gunakan /cancel untuk membatalkan\\.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return AUTO_CHANNEL


async def auto_view_receive_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima channel untuk auto view boost"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Akses Ditolak\\.", parse_mode='Markdown')
        return ConversationHandler.END
    
    channels_input = update.message.text.strip()
    
    if not channels_input:
        await update.message.reply_text(
            "❌ Channel tidak valid\\! Silakan kirim username channel yang valid\\.",
            parse_mode='Markdown'
        )
        return AUTO_CHANNEL
    
    # Parse channels
    channels = []
    for line in channels_input.split('\n'):
        for ch in line.split(','):
            ch = ch.strip()
            if ch:
                channel_name, _ = parse_channel_input(ch)
                if channel_name:
                    channels.append(channel_name)
    
    # Hapus duplikat
    seen = set()
    unique_channels = []
    for ch in channels:
        if ch.lower() not in seen:
            seen.add(ch.lower())
            unique_channels.append(ch)
    
    if not unique_channels:
        await update.message.reply_text(
            "❌ Tidak ada channel valid\\! Silakan coba lagi\\.",
            parse_mode='Markdown'
        )
        return AUTO_CHANNEL
    
    context.user_data['auto_channels'] = unique_channels
    num_channels = len(unique_channels)
    
    # Ambil sessions
    session_files = []
    if os.path.exists(SESSION_DIR):
        for file in os.listdir(SESSION_DIR):
            if file.endswith('.session') and not file.endswith('-journal'):
                phone = file.replace('.session', '')
                session_files.append(phone)
    
    context.user_data['auto_session_files'] = session_files
    total_sessions = len(session_files)
    
    # Hitung max session per channel (sessions dibagi rata ke semua channel, tidak overlap)
    max_per_channel = total_sessions // num_channels if num_channels > 0 else 0
    context.user_data['auto_max_per_channel'] = max_per_channel
    
    if max_per_channel < 1:
        keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="automation_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"❌ **Session tidak mencukupi\\!**\n\n"
            f"📺 Channel: **{num_channels}** channel\n"
            f"📊 Sessions: **{total_sessions}** akun\n\n"
            f"Minimal dibutuhkan **{num_channels}** session "
            f"\\(1 per channel\\)\\.\n"
            f"Silakan login lebih banyak akun terlebih dahulu\\.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    channels_text = '\n'.join([f"• `{escape_markdown(ch, version=1)}`" for ch in unique_channels])
    
    # Pilihan jumlah sesi PER CHANNEL
    keyboard = []
    session_options = []
    if max_per_channel >= 10:
        session_options.append(10)
    if max_per_channel >= 25:
        session_options.append(25)
    if max_per_channel >= 50:
        session_options.append(50)
    if max_per_channel >= 100:
        session_options.append(100)
    if max_per_channel >= 200:
        session_options.append(200)
    if max_per_channel >= 300:
        session_options.append(300)
    
    row = []
    for opt in session_options:
        row.append(InlineKeyboardButton(f"📱 {opt}", callback_data=f"auto_sc:{opt}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([
        InlineKeyboardButton(f"📱 Max ({max_per_channel})", callback_data="auto_sc:all"),
        InlineKeyboardButton("✏️ Custom", callback_data="auto_sc:custom")
    ])
    keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ **Channel diterima:**\n{channels_text}\n\n"
        f"📊 **Sessions tersedia:** {total_sessions} akun\n"
        f"📺 **Channel:** {num_channels} channel\n"
        f"📱 **Max per channel:** {max_per_channel} akun\n\n"
        f"⚠️ Setiap channel akan menggunakan **session yang berbeda** \\(tidak overlap\\)\\.\n"
        f"Total: per\\_channel × {num_channels} channel ≤ {total_sessions} session\n\n"
        f"**Pilih jumlah sesi PER CHANNEL:**",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return AUTO_SESSION_COUNT


async def auto_view_receive_session_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima jumlah sesi PER CHANNEL untuk auto view boost dari callback"""
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("❌ Akses Ditolak\\.", parse_mode='Markdown')
        return ConversationHandler.END
    
    data = query.data.replace("auto_sc:", "")
    session_files = context.user_data.get('auto_session_files', [])
    channels = context.user_data.get('auto_channels', [])
    num_channels = len(channels)
    max_per_channel = context.user_data.get('auto_max_per_channel', len(session_files) // max(num_channels, 1))
    
    if data == "custom":
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"✏️ **Custom Session Per Channel**\n\n"
            f"📊 Sessions tersedia: **{len(session_files)}** akun\n"
            f"📺 Channel: **{num_channels}** channel\n"
            f"📱 Max per channel: **{max_per_channel}** akun\n\n"
            f"Ketik jumlah sesi per channel \\(1\\-{max_per_channel}\\)\\.\n"
            f"Total yang digunakan: input × {num_channels} channel",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return AUTO_SESSION_COUNT
    
    if data == "all":
        per_channel = max_per_channel
    else:
        try:
            per_channel = int(data)
        except ValueError:
            per_channel = max_per_channel
    
    per_channel = min(per_channel, max_per_channel)
    per_channel = max(1, per_channel)
    context.user_data['auto_session_per_channel'] = per_channel
    
    # Lanjut ke pilihan interval
    return await auto_view_ask_interval(query, context)


async def auto_view_receive_session_count_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima jumlah sesi PER CHANNEL dari text input"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Akses Ditolak\\.", parse_mode='Markdown')
        return ConversationHandler.END
    
    text = update.message.text.strip()
    session_files = context.user_data.get('auto_session_files', [])
    channels = context.user_data.get('auto_channels', [])
    num_channels = len(channels)
    max_per_channel = context.user_data.get('auto_max_per_channel', len(session_files) // max(num_channels, 1))
    
    try:
        per_channel = int(text)
        if per_channel < 1:
            raise ValueError
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"❌ Input tidak valid\\! Masukkan angka 1\\-{max_per_channel}\\.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return AUTO_SESSION_COUNT
    
    if per_channel > max_per_channel:
        total_needed = per_channel * num_channels
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"❌ **Session tidak mencukupi\\!**\n\n"
            f"Anda memasukkan: **{per_channel}** per channel\n"
            f"Total dibutuhkan: {per_channel} × {num_channels} = **{total_needed}** session\n"
            f"Session tersedia: **{len(session_files)}** akun\n\n"
            f"📱 Max per channel: **{max_per_channel}**\n"
            f"Silakan masukkan angka 1\\-{max_per_channel}\\.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return AUTO_SESSION_COUNT
    
    context.user_data['auto_session_per_channel'] = per_channel
    
    # Tampilkan pilihan interval sebagai message (bukan edit)
    channels_str = ', '.join([f"`{escape_markdown(ch, version=1)}`" for ch in channels])
    total_used = per_channel * num_channels
    
    keyboard = [
        [
            InlineKeyboardButton("1 menit", callback_data="auto_interval:1"),
            InlineKeyboardButton("3 menit", callback_data="auto_interval:3"),
            InlineKeyboardButton("5 menit", callback_data="auto_interval:5"),
        ],
        [
            InlineKeyboardButton("10 menit", callback_data="auto_interval:10"),
            InlineKeyboardButton("15 menit", callback_data="auto_interval:15"),
            InlineKeyboardButton("30 menit", callback_data="auto_interval:30"),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data="auto_interval:custom")],
        [InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ **Konfigurasi Auto View Boost:**\n\n"
        f"📺 Channel: {channels_str}\n"
        f"👥 Sessions per channel: **{per_channel}** akun\n"
        f"📊 Total session: **{total_used}** / {len(session_files)} akun\n\n"
        f"⏱️ **Pilih interval pengecekan:**\n\n"
        f"Seberapa sering bot memonitor channel untuk post baru?\n"
        f"\\(Setiap channel menggunakan session yang berbeda\\)",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return AUTO_INTERVAL


async def auto_view_ask_interval(query, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan pilihan interval monitoring"""
    channels = context.user_data.get('auto_channels', [])
    per_channel = context.user_data.get('auto_session_per_channel', 0)
    session_files = context.user_data.get('auto_session_files', [])
    num_channels = len(channels)
    total_used = per_channel * num_channels
    channels_str = ', '.join([f"`{escape_markdown(ch, version=1)}`" for ch in channels])
    
    keyboard = [
        [
            InlineKeyboardButton("1 menit", callback_data="auto_interval:1"),
            InlineKeyboardButton("3 menit", callback_data="auto_interval:3"),
            InlineKeyboardButton("5 menit", callback_data="auto_interval:5"),
        ],
        [
            InlineKeyboardButton("10 menit", callback_data="auto_interval:10"),
            InlineKeyboardButton("15 menit", callback_data="auto_interval:15"),
            InlineKeyboardButton("30 menit", callback_data="auto_interval:30"),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data="auto_interval:custom")],
        [InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ **Konfigurasi Auto View Boost:**\n\n"
        f"📺 Channel: {channels_str}\n"
        f"👥 Sessions per channel: **{per_channel}** akun\n"
        f"📊 Total session: **{total_used}** / {len(session_files)} akun\n\n"
        f"⏱️ **Pilih interval pengecekan:**\n\n"
        f"Seberapa sering bot memonitor channel untuk post baru?\n"
        f"\\(Setiap channel menggunakan session yang berbeda\\)",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return AUTO_INTERVAL


async def auto_view_receive_interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima interval dari callback"""
    query = update.callback_query
    await query.answer()
    
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("❌ Akses Ditolak\\.", parse_mode='Markdown')
        return ConversationHandler.END
    
    data = query.data.replace("auto_interval:", "")
    
    if data == "custom":
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"✏️ **Custom Interval**\n\n"
            f"Ketik interval dalam menit \\({AUTO_CHECK_INTERVAL_MIN}\\-{AUTO_CHECK_INTERVAL_MAX}\\)\\.\n"
            f"Contoh: `5`",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return AUTO_INTERVAL
    
    try:
        interval = int(data)
    except ValueError:
        interval = AUTO_CHECK_DEFAULT
    
    interval = max(AUTO_CHECK_INTERVAL_MIN, min(interval, AUTO_CHECK_INTERVAL_MAX))
    
    # Mulai auto view boost
    await start_auto_view_monitor(query, context, interval)
    return ConversationHandler.END


async def auto_view_receive_interval_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menerima interval dari text input"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Akses Ditolak\\.", parse_mode='Markdown')
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    try:
        interval = int(text)
        if interval < AUTO_CHECK_INTERVAL_MIN or interval > AUTO_CHECK_INTERVAL_MAX:
            raise ValueError
    except ValueError:
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel_auto_view")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"❌ Input tidak valid\\! Masukkan angka {AUTO_CHECK_INTERVAL_MIN}\\-{AUTO_CHECK_INTERVAL_MAX}\\.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return AUTO_INTERVAL
    
    # Mulai auto view boost dari message
    await start_auto_view_monitor_from_message(update, context, interval)
    return ConversationHandler.END


def _build_channel_pools(channels, session_files, per_channel):
    """Membagi session ke pool per channel (tidak overlap).
    
    Contoh: 2 channel, 600 session, 300 per channel:
      channel_A → sessions[0:300]   (monitor = sessions[0])
      channel_B → sessions[300:600] (monitor = sessions[300])
    
    Returns: (channel_session_pools, channel_monitors)
    """
    channel_session_pools = {}  # channel -> list of sessions untuk boost
    channel_monitors = {}       # channel -> session phone untuk monitoring
    
    offset = 0
    for channel in channels:
        pool = session_files[offset:offset + per_channel]
        channel_session_pools[channel] = pool
        channel_monitors[channel] = pool[0] if pool else None  # session pertama = monitor
        offset += per_channel
    
    return channel_session_pools, channel_monitors


async def start_auto_view_monitor(query, context: ContextTypes.DEFAULT_TYPE, interval: int):
    """Memulai auto view boost monitor dari callback"""
    chat_id = query.message.chat_id
    channels = context.user_data.get('auto_channels', [])
    per_channel = context.user_data.get('auto_session_per_channel', 0)
    session_files = context.user_data.get('auto_session_files', [])
    num_channels = len(channels)
    total_used = per_channel * num_channels
    
    if total_used < 1 or total_used > len(session_files):
        await query.edit_message_text(
            "❌ Konfigurasi session tidak valid\\.",
            parse_mode='Markdown'
        )
        return
    
    # Bagi session ke pool per channel (tidak overlap)
    channel_session_pools, channel_monitors = _build_channel_pools(
        channels, session_files, per_channel
    )
    
    channels_str = ', '.join([f"`{escape_markdown(ch, version=1)}`" for ch in channels])
    
    # Buat teks pool info per channel
    pool_info_lines = []
    for ch in channels:
        ch_esc = escape_markdown(ch, version=1)
        pool = channel_session_pools[ch]
        monitor = channel_monitors[ch]
        monitor_esc = escape_markdown(monitor, version=1) if monitor else "\\-"
        pool_info_lines.append(
            f"  `{ch_esc}`: **{len(pool)}** session \\(monitor: `{monitor_esc}`\\)"
        )
    pool_info = '\n'.join(pool_info_lines)
    
    # Simpan state monitor
    auto_monitors[chat_id] = {
        'channels': channels,
        'per_channel': per_channel,
        'total_sessions': total_used,
        'channel_session_pools': channel_session_pools,  # pool terpisah per channel
        'channel_monitors': channel_monitors,             # monitor session per channel
        'interval': interval,
        'last_post_ids': {},
        'task': None,
        'active': True,
        'started_at': time.time(),
        'boost_count': 0,
    }
    
    keyboard = [
        [InlineKeyboardButton("🔴 Stop Auto View", callback_data="stop_auto_view")],
        [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🟢 **Auto View Boost Aktif\\!**\n\n"
        f"📺 Channel: {channels_str}\n"
        f"👥 Sessions per channel: **{per_channel}** akun\n"
        f"📊 Total session: **{total_used}** akun\n"
        f"⏱️ Interval: **{interval}** menit\n\n"
        f"🔍 **Pool per Channel:**\n{pool_info}\n\n"
        f"✅ Setiap channel menggunakan session yang **berbeda** \\(tidak overlap\\)\\.\n"
        f"✅ Semua channel akan di\\-boost **bersamaan** saat ada post baru\\.\n\n"
        f"Tekan **Stop Auto View** untuk menghentikan\\.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # Mulai background task
    app = context.application
    task = asyncio.create_task(
        auto_view_monitor_loop(chat_id, app)
    )
    auto_monitors[chat_id]['task'] = task
    
    context.user_data.clear()


async def start_auto_view_monitor_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE, interval: int):
    """Memulai auto view boost monitor dari text message"""
    chat_id = update.effective_chat.id
    channels = context.user_data.get('auto_channels', [])
    per_channel = context.user_data.get('auto_session_per_channel', 0)
    session_files = context.user_data.get('auto_session_files', [])
    num_channels = len(channels)
    total_used = per_channel * num_channels
    
    if total_used < 1 or total_used > len(session_files):
        await update.message.reply_text(
            "❌ Konfigurasi session tidak valid\\.",
            parse_mode='Markdown'
        )
        return
    
    # Bagi session ke pool per channel (tidak overlap)
    channel_session_pools, channel_monitors = _build_channel_pools(
        channels, session_files, per_channel
    )
    
    channels_str = ', '.join([f"`{escape_markdown(ch, version=1)}`" for ch in channels])
    
    # Buat teks pool info per channel
    pool_info_lines = []
    for ch in channels:
        ch_esc = escape_markdown(ch, version=1)
        pool = channel_session_pools[ch]
        monitor = channel_monitors[ch]
        monitor_esc = escape_markdown(monitor, version=1) if monitor else "\\-"
        pool_info_lines.append(
            f"  `{ch_esc}`: **{len(pool)}** session \\(monitor: `{monitor_esc}`\\)"
        )
    pool_info = '\n'.join(pool_info_lines)
    
    auto_monitors[chat_id] = {
        'channels': channels,
        'per_channel': per_channel,
        'total_sessions': total_used,
        'channel_session_pools': channel_session_pools,
        'channel_monitors': channel_monitors,
        'interval': interval,
        'last_post_ids': {},
        'task': None,
        'active': True,
        'started_at': time.time(),
        'boost_count': 0,
    }
    
    keyboard = [
        [InlineKeyboardButton("🔴 Stop Auto View", callback_data="stop_auto_view")],
        [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🟢 **Auto View Boost Aktif\\!**\n\n"
        f"📺 Channel: {channels_str}\n"
        f"👥 Sessions per channel: **{per_channel}** akun\n"
        f"📊 Total session: **{total_used}** akun\n"
        f"⏱️ Interval: **{interval}** menit\n\n"
        f"🔍 **Pool per Channel:**\n{pool_info}\n\n"
        f"✅ Setiap channel menggunakan session yang **berbeda** \\(tidak overlap\\)\\.\n"
        f"✅ Semua channel akan di\\-boost **bersamaan** saat ada post baru\\.\n\n"
        f"Tekan **Stop Auto View** untuk menghentikan\\.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    app = context.application
    task = asyncio.create_task(
        auto_view_monitor_loop(chat_id, app)
    )
    auto_monitors[chat_id]['task'] = task
    
    context.user_data.clear()


async def _check_channel_for_new_posts(channel: str, monitor_phone: str, monitor: dict):
    """Cek post baru di satu channel menggunakan session monitor-nya.
    
    Returns: (channel, new_posts_list) atau (channel, None) jika tidak ada post baru.
    """
    auth = None
    try:
        auth = TelethonAuth(monitor_phone)
        if not auth.is_session_exists():
            print(f"[AUTO VIEW] Monitor session not found for {channel}: {monitor_phone}")
            return channel, None
        
        connected = await auth.connect()
        if not connected:
            print(f"[AUTO VIEW] Failed to connect monitor for {channel}: {monitor_phone}")
            return channel, None
        
        last_known_id = monitor['last_post_ids'].get(channel.lower())
        
        if last_known_id is None:
            # Pertama kali / belum ada baseline: ambil post terbaru sebagai baseline
            latest_post, error = await auth.get_latest_post(channel)
            if latest_post:
                monitor['last_post_ids'][channel.lower()] = latest_post.id
                print(f"[AUTO VIEW] Set baseline for {channel}: {latest_post.id} (via {monitor_phone})")
            elif error:
                print(f"[AUTO VIEW] Error getting baseline for {channel}: {error}")
            return channel, None
        
        # Ambil SEMUA post baru sejak last_known_id (sudah difilter service messages)
        new_posts, error = await auth.get_new_posts_since(channel, last_known_id)
        
        if error:
            print(f"[AUTO VIEW] Error checking {channel} (via {monitor_phone}): {error}")
            return channel, None
        
        if not new_posts:
            print(f"[AUTO VIEW] {channel}: No new posts (last_known={last_known_id}, monitor={monitor_phone})")
            return channel, None
        
        # Ada post baru! Update baseline ke post terbaru
        newest_id = new_posts[-1].id
        monitor['last_post_ids'][channel.lower()] = newest_id
        print(f"[AUTO VIEW] {channel}: {len(new_posts)} new post(s) detected! IDs: {[p.id for p in new_posts]} (via {monitor_phone})")
        
        return channel, new_posts
    
    except Exception as e:
        print(f"[AUTO VIEW] Error checking channel {channel}: {e}")
        return channel, None
    finally:
        if auth:
            try:
                await auth.disconnect()
            except Exception:
                pass


async def _boost_channel_posts(channel: str, new_posts: list, session_pool: list,
                                monitor: dict, chat_id: int, app, interval: int):
    """Boost semua post baru di satu channel menggunakan pool session-nya sendiri.
    Fungsi ini dijalankan secara paralel untuk setiap channel.
    """
    channel_escaped = escape_markdown(channel, version=1)
    
    for new_post in new_posts:
        if not monitor.get('active', False):
            break
        
        current_id = new_post.id
        post_link = f"https://t.me/{channel}/{current_id}"
        
        # Identifikasi jenis post
        post_type = "📝 Text"
        if new_post.media:
            media_type = type(new_post.media).__name__
            if "Photo" in media_type:
                post_type = "🖼️ Foto"
            elif "Video" in media_type or "Document" in media_type:
                post_type = "🎬 Video/File"
            else:
                post_type = "📎 Media"
        
        # Kirim notifikasi
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 **Post Baru Terdeteksi\\!**\n\n"
                    f"📺 Channel: `{channel_escaped}`\n"
                    f"📝 Post ID: {current_id}\n"
                    f"📌 Tipe: {post_type}\n"
                    f"👥 Pool: **{len(session_pool)}** session\n"
                    f"🔗 [Link Post]({post_link})\n\n"
                    f"⏳ Memulai view boosting otomatis\\.\\.\\."
                ),
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"[AUTO VIEW] Error sending notification: {e}")
        
        # Jalankan view boosting dengan pool session khusus channel ini
        try:
            results, boosted_post_id = await process_view_boosting(
                session_pool, channel, current_id
            )
            
            success_count = sum(1 for r in results if r['success'])
            failed_count = len(results) - success_count
            monitor['boost_count'] = monitor.get('boost_count', 0) + 1
            
            # Kirim hasil
            keyboard = [
                [InlineKeyboardButton("🔴 Stop Auto View", callback_data="stop_auto_view")],
                [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ **Auto View Boost Selesai\\!**\n\n"
                        f"📺 Channel: `{channel_escaped}`\n"
                        f"📝 Post ID: {current_id}\n"
                        f"📌 Tipe: {post_type}\n"
                        f"🔗 [Link Post]({post_link})\n\n"
                        f"✅ Berhasil: **{success_count}** views\n"
                        f"❌ Gagal: **{failed_count}** akun\n"
                        f"👥 Pool: **{len(session_pool)}** session\n"
                        f"📊 Total boost: **{monitor.get('boost_count', 0)}** kali\n\n"
                        f"⏱️ Cek berikutnya: {interval} menit lagi"
                    ),
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            except Exception as e:
                print(f"[AUTO VIEW] Error sending result for {channel}: {e}")
                
        except Exception as e:
            print(f"[AUTO VIEW] Error during view boosting for {channel} post {current_id}: {e}")
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ **Error Auto View Boost:**\n\n"
                        f"Channel: `{channel_escaped}`\n"
                        f"Post ID: {current_id}\n"
                        f"Error: `{escape_markdown(str(e), version=1)}`"
                    ),
                    parse_mode='Markdown'
                )
            except Exception:
                pass
        
        # Delay antar boost post yang berbeda dalam channel yang sama
        if len(new_posts) > 1:
            await asyncio.sleep(3)


async def auto_view_monitor_loop(chat_id: int, app):
    """Background task: monitor channel dan auto boost jika ada post baru
    
    Setiap channel memiliki:
    - Session MONITOR terpisah (untuk cek post baru)
    - Session POOL terpisah (untuk boosting, tidak overlap antar channel)
    
    Jika terdeteksi post baru di beberapa channel sekaligus,
    view boosting berjalan BERSAMAAN (parallel) menggunakan pool masing-masing.
    """
    monitor = auto_monitors.get(chat_id)
    if not monitor:
        return
    
    channels = monitor['channels']
    interval = monitor['interval']
    channel_monitors = monitor['channel_monitors']           # dict: channel -> monitor session
    channel_session_pools = monitor['channel_session_pools'] # dict: channel -> [session list]
    
    # Log info
    for ch in channels:
        pool = channel_session_pools.get(ch, [])
        mon = channel_monitors.get(ch, '?')
        print(f"[AUTO VIEW] Pool: {ch} → {len(pool)} sessions (monitor: {mon})")
    print(f"[AUTO VIEW] Monitor started for chat {chat_id}, interval: {interval}m")
    
    # ============ FASE BASELINE (paralel per channel) ============
    async def _get_baseline(channel, monitor_phone):
        auth = None
        try:
            auth = TelethonAuth(monitor_phone)
            if auth.is_session_exists():
                connected = await auth.connect()
                if connected:
                    latest_post, error = await auth.get_latest_post(channel)
                    if latest_post:
                        monitor['last_post_ids'][channel.lower()] = latest_post.id
                        print(f"[AUTO VIEW] Baseline for {channel}: post #{latest_post.id} (via {monitor_phone})")
                    elif error:
                        print(f"[AUTO VIEW] Error getting baseline for {channel}: {error}")
        except Exception as e:
            print(f"[AUTO VIEW] Error during baseline for {channel}: {e}")
        finally:
            if auth:
                try:
                    await auth.disconnect()
                except Exception:
                    pass
    
    # Ambil baseline semua channel secara paralel
    baseline_tasks = []
    for channel in channels:
        monitor_phone = channel_monitors.get(channel)
        if monitor_phone:
            baseline_tasks.append(_get_baseline(channel, monitor_phone))
    
    if baseline_tasks:
        await asyncio.gather(*baseline_tasks, return_exceptions=True)
    
    # ============ LOOP MONITORING ============
    while monitor.get('active', False):
        try:
            # Tunggu interval
            await asyncio.sleep(interval * 60)
            
            # Cek apakah masih aktif setelah sleep
            if not monitor.get('active', False):
                break
            
            print(f"[AUTO VIEW] Checking for new posts across {len(channels)} channel(s)...")
            
            # STEP 1: Cek semua channel secara PARALEL
            check_tasks = []
            for channel in channels:
                if not monitor.get('active', False):
                    break
                monitor_phone = channel_monitors.get(channel)
                if monitor_phone:
                    check_tasks.append(
                        _check_channel_for_new_posts(channel, monitor_phone, monitor)
                    )
            
            if not check_tasks:
                continue
            
            check_results = await asyncio.gather(*check_tasks, return_exceptions=True)
            
            # STEP 2: Kumpulkan channel yang punya post baru
            channels_to_boost = []  # [(channel, new_posts), ...]
            for result in check_results:
                if isinstance(result, Exception):
                    print(f"[AUTO VIEW] Check task error: {result}")
                    continue
                channel, new_posts = result
                if new_posts:
                    channels_to_boost.append((channel, new_posts))
            
            if not channels_to_boost:
                print(f"[AUTO VIEW] No new posts in any channel")
                continue
            
            print(f"[AUTO VIEW] {len(channels_to_boost)} channel(s) have new posts, boosting in PARALLEL...")
            
            # STEP 3: Boost semua channel yang punya post baru secara BERSAMAAN
            boost_tasks = []
            for channel, new_posts in channels_to_boost:
                if not monitor.get('active', False):
                    break
                session_pool = channel_session_pools.get(channel, [])
                if not session_pool:
                    print(f"[AUTO VIEW] No session pool for {channel}, skipping boost")
                    continue
                
                boost_tasks.append(
                    _boost_channel_posts(
                        channel, new_posts, session_pool,
                        monitor, chat_id, app, interval
                    )
                )
            
            if boost_tasks:
                # Jalankan semua boost secara paralel!
                await asyncio.gather(*boost_tasks, return_exceptions=True)
        
        except asyncio.CancelledError:
            print(f"[AUTO VIEW] Monitor task cancelled for chat {chat_id}")
            break
        except Exception as e:
            print(f"[AUTO VIEW] Unexpected error in monitor loop: {e}")
            await asyncio.sleep(30)
    
    print(f"[AUTO VIEW] Monitor stopped for chat {chat_id}")


async def stop_auto_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menghentikan auto view boost monitor"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if not is_admin(user_id):
        await query.edit_message_text("❌ Akses Ditolak\\.", parse_mode='Markdown')
        return
    
    monitor = auto_monitors.get(chat_id)
    
    if not monitor or not monitor.get('active', False):
        keyboard = [[InlineKeyboardButton("🔙 Menu Automation", callback_data="automation_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "ℹ️ Tidak ada Auto View Boost yang aktif\\.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Stop monitor
    monitor['active'] = False
    task = monitor.get('task')
    if task and not task.done():
        task.cancel()
    
    channels_str = ', '.join(monitor.get('channels', []))
    boost_count = monitor.get('boost_count', 0)
    per_channel = monitor.get('per_channel', 0)
    total_sessions = monitor.get('total_sessions', 0)
    started = monitor.get('started_at', 0)
    elapsed = int(time.time() - started) if started else 0
    elapsed_min = elapsed // 60
    elapsed_sec = elapsed % 60
    
    # Hapus dari state
    del auto_monitors[chat_id]
    
    keyboard = [
        [InlineKeyboardButton("🔙 Menu Automation", callback_data="automation_menu")],
        [InlineKeyboardButton("🗑️ Hapus", callback_data="delete_message")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🔴 **Auto View Boost Dihentikan**\n\n"
        f"📺 Channel: `{escape_markdown(channels_str, version=1)}`\n"
        f"👥 Sessions: **{per_channel}** per channel \\(**{total_sessions}** total\\)\n"
        f"📊 Total boost: **{boost_count}** kali\n"
        f"⏳ Durasi: {elapsed_min}m {elapsed_sec}s",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def cancel_auto_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan setup auto view boost"""
    query = update.callback_query
    if query:
        await query.answer()
        context.user_data.clear()
        await query.edit_message_text(
            "❌ Setup Auto View Boost dibatalkan\\.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Kembali", callback_data="automation_menu")]
            ]),
            parse_mode='Markdown'
        )
    else:
        context.user_data.clear()
        await update.message.reply_text("❌ Setup Auto View Boost dibatalkan\\.", parse_mode='Markdown')
    return ConversationHandler.END


# =============================================
# SHUTDOWN / CLEANUP
# =============================================

async def cleanup_auto_monitors():
    """Membersihkan semua auto monitor tasks saat bot shutdown.
    Dipanggil dari bot.py post_shutdown callback.
    """
    # 1. Cleanup auto view monitor background tasks
    print("[AUTO VIEW] Cleaning up all auto monitor tasks...")
    for chat_id, monitor in list(auto_monitors.items()):
        monitor['active'] = False
        task = monitor.get('task')
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        print(f"[AUTO VIEW] Monitor for chat {chat_id} stopped")
    auto_monitors.clear()
    print("[AUTO VIEW] All monitors cleaned up")
    
    # 2. Cleanup active view boosting tasks (manual view boost)
    if active_view_tasks:
        print(f"[VIEW BOOST] Cleaning up {len(active_view_tasks)} active view task(s)...")
        for task in list(active_view_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        active_view_tasks.clear()
        print("[VIEW BOOST] All active view tasks cleaned up")


# =============================================
# CANCEL & HANDLERS
# =============================================

async def cancel_automation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan automation"""
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
        return ConversationHandler.END
    
    context.user_data.clear()
    
    await query.edit_message_text(
        "❌ Automation dibatalkan.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="automation_menu")]])
    )
    
    return ConversationHandler.END


def get_automation_handlers():
    """Mengembalikan handlers untuk automation"""
    view_boosting_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(view_boosting_start, pattern="^view_boosting$")],
        states={
            CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel)],
            SESSION_COUNT: [
                CallbackQueryHandler(receive_session_count_callback, pattern="^session_count:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_session_count_text)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_automation),
            CallbackQueryHandler(cancel_automation, pattern="^cancel_automation$")
        ],
        per_chat=True,
        per_user=True,
        per_message=False
    )
    
    auto_view_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(auto_view_start, pattern="^auto_view_start$")],
        states={
            AUTO_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, auto_view_receive_channel)],
            AUTO_SESSION_COUNT: [
                CallbackQueryHandler(auto_view_receive_session_count_callback, pattern="^auto_sc:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, auto_view_receive_session_count_text)
            ],
            AUTO_INTERVAL: [
                CallbackQueryHandler(auto_view_receive_interval_callback, pattern="^auto_interval:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, auto_view_receive_interval_text)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_auto_view),
            CallbackQueryHandler(cancel_auto_view, pattern="^cancel_auto_view$")
        ],
        per_chat=True,
        per_user=True,
        per_message=False
    )
    
    return [
        CallbackQueryHandler(automation_menu, pattern="^automation_menu$"),
        CallbackQueryHandler(stop_auto_view, pattern="^stop_auto_view$"),
        view_boosting_conv,
        auto_view_conv,
    ]

