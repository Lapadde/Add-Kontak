import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    UserAlreadyParticipantError,
    UserNotParticipantError,
    ChatAdminRequiredError,
    PeerFloodError,
    AuthKeyNotFound,
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    AuthKeyError,
)
from telethon.tl.functions.channels import (
    JoinChannelRequest,
    InviteToChannelRequest,
    GetParticipantRequest,
)
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.messages import (
    GetMessagesViewsRequest,
    ImportChatInviteRequest,
    CheckChatInviteRequest,
    AddChatUserRequest,
)
from telethon.tl.types import (
    User,
    MessageService,
    Channel,
    Chat,
    InputUser,
    InputPeerSelf,
    ChatInviteAlready,
    PeerChannel,
    PeerChat,
    UserStatusEmpty,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
    UpdateChannelParticipant,
)
import os
from config import API_ID, API_HASH, SESSION_DIR

logger = logging.getLogger(__name__)

# Undangan non-mutual: jeda antar tiap kontak (channel & grup klasik)
NON_MUTUAL_INVITE_DELAY_SEC = 5.0
# FloodWait di atas ini (detik) menghentikan undangan; di bawah/sama: tunggu lalu ulangi request
FLOOD_WAIT_ABORT_ABOVE_SEC = 300  # 5 menit
# Saat PeerFloodError (anti-spam permanen) → ditandai dengan sentinel ini supaya
# pemanggil mengeluarkan akun dari pool. Nilainya >> FLOOD_WAIT_ABORT_ABOVE_SEC.
PEER_FLOOD_COOLDOWN_SEC = 24 * 60 * 60  # 24 jam


def _is_auth_key_error(exc: BaseException) -> bool:
    """True bila exception menunjukkan auth key tidak lagi valid di server.

    Server bisa "melupakan" auth key kapan saja (sesuai pesan resmi Telethon).
    Skenario ini mencakup:
    - ``AuthKeyNotFound`` (common): server tidak mengenali key (paling sering
      muncul saat awal connect).
    - ``AuthKeyUnregisteredError``: key tidak terdaftar lagi (mis. user telah
      menghentikan session ini dari Settings → Devices).
    - ``AuthKeyDuplicatedError``: key dipakai 2 client sekaligus → server
      otomatis invalidasi.
    - ``AuthKeyError`` (umum / unknown auth).

    Fallback: cek string error untuk kasus pesan generic dari Telethon.
    """
    if isinstance(
        exc,
        (AuthKeyNotFound, AuthKeyUnregisteredError, AuthKeyDuplicatedError, AuthKeyError),
    ):
        return True
    msg = (str(exc) or "").lower()
    return (
        "auth_key" in msg
        or "auth key" in msg
        or "authkey" in msg
        or "authorization key" in msg
    )


def _is_updates_parsing_bug(exc: BaseException) -> bool:
    """True bila exception ini adalah bug Telethon "'Updates' object is not iterable".

    Bug terjadi di internal updater Telethon ketika memproses respons RPC
    tertentu. Request ke server BIASANYA sudah diterima Telegram (user
    kemungkinan besar berhasil diundang). Kita perlakukan sebagai sukses agar
    user tidak di-drop tanpa alasan.
    """
    if not isinstance(exc, TypeError):
        return False
    msg = str(exc).lower()
    return "updates" in msg and "not iterable" in msg
# Jika grup sumber menyembunyikan member, fallback baca riwayat pesan & ambil pengirim unik.
# Default tetap (tidak dapat diubah dari UI admin).
HIDDEN_MEMBERS_MSG_FALLBACK_LIMIT = 1000


def _count_invited_users_from_invite_updates(result, batch_users: list) -> int:
    """Hitung user yang muncul di UpdateChannelParticipant; fallback len(batch) jika update tidak terbaca."""
    if not batch_users:
        return 0
    batch_ids = {u.id for u in batch_users if isinstance(u, User)}
    if not batch_ids:
        return len(batch_users)
    updates = getattr(result, "updates", None) if result is not None else None
    if not updates:
        return len(batch_users)
    matched = set()
    for up in updates:
        if isinstance(up, UpdateChannelParticipant):
            uid = getattr(up, "user_id", None)
            if uid is not None and uid in batch_ids:
                matched.add(uid)
    if matched:
        return len(matched)
    return len(batch_users)


class TelethonAuth:
    def __init__(self, phone_number: str):
        self.phone_number = phone_number
        # Gunakan extension .session untuk konsistensi dan keamanan
        # Tapi isi file adalah string session (text format)
        self.session_path = os.path.join(SESSION_DIR, f"{phone_number}.session")
        self.string_session = None
        
        # Load string session jika sudah ada
        if os.path.exists(self.session_path):
            try:
                with open(self.session_path, 'r', encoding='utf-8') as f:
                    self.string_session = f.read().strip()
            except Exception:
                # Jika gagal baca sebagai text, mungkin file binary lama
                # Coba load sebagai binary session (backward compatibility)
                pass
        
        # Buat client dengan StringSession
        # Gunakan connection_retries dan timeout untuk stabilitas
        self.client = TelegramClient(
            StringSession(self.string_session) if self.string_session else StringSession(),
            API_ID,
            API_HASH,
            connection_retries=3,
            retry_delay=1,
            timeout=30
        )
        self.otp_code = None
        self.password = None
        self.is_connected = False

    def _rebuild_client_blank_session(self):
        """Buat ulang ``self.client`` dengan StringSession kosong.

        Dipakai setelah server tidak lagi mengenali auth key lama. File
        ``.session`` di disk juga dihapus untuk mencegah load ulang key
        yang sama lagi pada start berikutnya.
        """
        try:
            if self.client is not None:
                try:
                    # Pastikan socket lama tertutup (sync best-effort; jika
                    # masih async-running, abaikan errornya).
                    if hasattr(self.client, "_sender") and self.client._sender:
                        pass
                except Exception:
                    pass
        finally:
            self.string_session = None
            try:
                if os.path.exists(self.session_path):
                    os.remove(self.session_path)
            except Exception:
                pass
            self.client = TelegramClient(
                StringSession(),
                API_ID,
                API_HASH,
                connection_retries=3,
                retry_delay=1,
                timeout=30,
            )
            self.is_connected = False

    async def connect(self):
        """Menghubungkan client ke Telegram.

        Bila server tidak mengenali auth key (``AuthKeyNotFound`` dan
        kerabatnya), file session lama dihapus otomatis dan client di-recreate
        sehingga alur login bisa dilanjutkan dengan OTP baru. Pemanggil bisa
        mengecek bendera ``self.auth_key_reset`` untuk memberi tahu user.
        """
        self.auth_key_reset = False
        try:
            await self.client.connect()
        except Exception as e:
            if _is_auth_key_error(e):
                # Server lupa key lama → bersihkan & coba connect ulang.
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self._rebuild_client_blank_session()
                self.auth_key_reset = True
                await self.client.connect()
            else:
                raise

        try:
            authorized = await self.client.is_user_authorized()
        except Exception as e:
            if _is_auth_key_error(e):
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self._rebuild_client_blank_session()
                self.auth_key_reset = True
                await self.client.connect()
                authorized = False
            else:
                raise

        if not authorized:
            await self.client.send_code_request(self.phone_number)
            return False
        self.is_connected = True
        return True
    
    async def check_session_validity(self):
        """Mengecek apakah session masih valid dan bisa digunakan
        
        Metode optimasi:
        1. connect() - Koneksi ke Telegram (tanpa send_code_request)
        2. is_user_authorized() - Filter cepat (cek tanpa network call berat)
        3. get_me() - Validasi final (hanya jika authorized)
        """
        try:
            # Step 1: Connect ke Telegram (langsung connect tanpa trigger send_code_request)
            if not self.is_connected:
                try:
                    await self.client.connect()
                    self.is_connected = True
                except Exception as e:
                    if _is_auth_key_error(e):
                        return False, (
                            "Auth key tidak dikenali server (session sudah "
                            "dilupakan / dihapus oleh Telegram). Perlu login ulang."
                        )
                    error_msg = str(e).lower()
                    if "unauthorized" in error_msg:
                        return False, "Session tidak valid (unauthorized)"
                    elif "timeout" in error_msg or "connection" in error_msg:
                        return False, "Timeout: Gagal terhubung ke Telegram"
                    else:
                        return False, f"Error koneksi: {str(e)}"

            # Step 2: Filter cepat dengan is_user_authorized()
            try:
                is_authorized = await self.client.is_user_authorized()
                if not is_authorized:
                    return False, "Session tidak valid (user tidak authorized)"
            except Exception as e:
                if _is_auth_key_error(e):
                    return False, (
                        "Auth key tidak dikenali server saat cek authorize. "
                        "Perlu login ulang."
                    )
                error_msg = str(e).lower()
                if "unauthorized" in error_msg:
                    return False, "Session tidak valid (unauthorized)"
                elif "flood" in error_msg:
                    return False, "Rate limit: Terlalu banyak request"
                else:
                    return False, f"Error cek authorization: {str(e)}"

            # Step 3: Validasi final dengan get_me()
            try:
                me = await self.client.get_me()
                if me:
                    return True, "Session valid"
                else:
                    return False, "Tidak dapat mengambil informasi user"
            except Exception as e:
                if _is_auth_key_error(e):
                    return False, (
                        "Auth key tidak dikenali server saat validasi user. "
                        "Perlu login ulang."
                    )
                error_msg = str(e).lower()
                if "unauthorized" in error_msg:
                    return False, "Session tidak valid (unauthorized)"
                elif "flood" in error_msg:
                    return False, "Rate limit: Terlalu banyak request"
                else:
                    return False, f"Error validasi: {str(e)}"
        except Exception as e:
            if _is_auth_key_error(e):
                return False, (
                    "Auth key tidak dikenali server. Session lama tidak "
                    "berlaku lagi; perlu login ulang."
                )
            error_msg = str(e).lower()
            if "unauthorized" in error_msg:
                return False, "Session tidak valid (unauthorized)"
            elif "timeout" in error_msg:
                return False, "Timeout: Gagal terhubung"
            else:
                return False, f"Error: {str(e)}"
    
    async def resend_code(self):
        """Mengirim ulang kode OTP"""
        try:
            await self.client.send_code_request(self.phone_number)
            return True, "Kode OTP baru telah dikirim!"
        except Exception as e:
            return False, f"Error saat mengirim ulang kode: {str(e)}"

    async def sign_in(self, otp_code: str = None, password: str = None):
        """Melakukan sign in dengan OTP dan password"""
        try:
            if not self.is_connected:
                if otp_code:
                    self.otp_code = otp_code
                if password:
                    self.password = password

                if self.otp_code:
                    try:
                        await self.client.sign_in(self.phone_number, self.otp_code)
                        self.is_connected = True
                        # Simpan string session setelah login berhasil
                        await self.save_string_session()
                        return True, "Login berhasil!"
                    except SessionPasswordNeededError:
                        if self.password:
                            await self.client.sign_in(password=self.password)
                            self.is_connected = True
                            # Simpan string session setelah login berhasil
                            await self.save_string_session()
                            return True, "Login berhasil!"
                        else:
                            return False, "Password diperlukan"
                    except PhoneCodeInvalidError:
                        return False, "Kode OTP tidak valid. Silakan coba lagi atau gunakan /resend untuk meminta kode baru."
                    except PhoneCodeExpiredError:
                        return False, "Kode OTP sudah kedaluwarsa. Gunakan /resend untuk meminta kode baru."
                    except Exception as e:
                        error_msg = str(e)
                        # Deteksi error "all available options"
                        if "all available options" in error_msg.lower() or "resendcode" in error_msg.lower():
                            return False, "RESEND_NEEDED"
                        return False, f"Error: {error_msg}"
                else:
                    return False, "OTP diperlukan"
            else:
                return True, "Sudah terhubung"
        except Exception as e:
            error_msg = str(e)
            if "all available options" in error_msg.lower() or "resendcode" in error_msg.lower():
                return False, "RESEND_NEEDED"
            return False, f"Error saat sign in: {error_msg}"

    async def get_me(self):
        """Mendapatkan informasi user yang login"""
        if self.is_connected:
            me = await self.client.get_me()
            return me
        return None

    async def disconnect(self):
        """Memutus koneksi client. Set bendera supaya pemanggil tidak salah baca state."""
        try:
            if self.client:
                await self.client.disconnect()
        finally:
            self.is_connected = False

    def is_session_exists(self):
        """True bila **file** .session ada dan isinya tampak seperti string session valid.

        Catatan: cek ini hanya berbasis **keberadaan & panjang** isi file, bukan
        validitas Telegram. Untuk verifikasi sungguhan (user masih authorized),
        gunakan ``check_session_validity()`` setelah ``connect()``.
        """
        # Cek file .session (berisi string session sebagai text)
        if os.path.exists(self.session_path):
            # Baca dan validasi string session
            try:
                with open(self.session_path, 'r', encoding='utf-8') as f:
                    session_str = f.read().strip()
                    if session_str and len(session_str) > 10:  # Minimal valid string session
                        return True
            except Exception:
                # Jika gagal baca sebagai text, mungkin file binary lama
                # Cek apakah file ada (untuk backward compatibility)
                if os.path.getsize(self.session_path) > 0:
                    return True
        return False

    async def save_string_session(self):
        """Menyimpan string session ke file .session (isi file adalah text string session)"""
        try:
            if self.is_connected:
                string_session = self.client.session.save()
                # Simpan string session ke file .session (extension .session, isi text)
                with open(self.session_path, 'w', encoding='utf-8') as f:
                    f.write(string_session)
                self.string_session = string_session
        except Exception as e:
            print(f"Warning: Gagal menyimpan string session: {e}")

    def cleanup_session(self):
        """Menghapus session file jika login gagal"""
        try:
            if os.path.exists(self.session_path):
                os.remove(self.session_path)
        except Exception as e:
            print(f"Warning: Gagal menghapus session file: {e}")

    async def get_latest_otp(self, limit=100):
        """Membaca OTP terbaru dari Telegram menggunakan get_messages(777000)"""
        should_disconnect = False
        try:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    return None, "Tidak dapat terhubung ke Telegram"
                should_disconnect = True  # Mark untuk disconnect jika kita yang connect
            
            # Ambil pesan dari Telegram Official (ID: 777000)
            messages = await self.client.get_messages(777000, limit=limit)
            
            otp_messages = []
            for message in messages:
                if not message or not message.text:
                    continue
                    
                text = message.text
                # Cari pola OTP (biasanya 5 digit angka, bisa juga 4-6 digit)
                otp_pattern = r'\b\d{4,6}\b'
                matches = re.findall(otp_pattern, text)
                
                # Filter untuk OTP yang lebih spesifik (biasanya 5 digit)
                valid_otp = [m for m in matches if len(m) == 5]
                
                # Cek jika ada kata kunci OTP atau verification code
                is_otp_message = (
                    'code' in text.lower() and 'verification' in text.lower()
                ) or (
                    'login code' in text.lower()
                ) or (
                    'your code' in text.lower() and any(len(m) == 5 for m in matches)
                ) or valid_otp
                
                if is_otp_message:
                    otp_messages.append({
                        'date': message.date,
                        'text': text,
                        'otp_codes': valid_otp if valid_otp else matches[:1]  # Ambil yang 5 digit atau pertama
                    })
            
            if otp_messages:
                # Ambil yang terbaru (sudah diurutkan dari terbaru)
                latest = otp_messages[0]
                return latest, None
            else:
                return None, "Tidak ada OTP ditemukan. Pastikan pesan OTP masih ada di chat Telegram."
                
        except Exception as e:
            return None, f"Error: {str(e)}"
        finally:
            # Hanya disconnect jika kita yang melakukan connect (bukan yang sudah connected sebelumnya)
            # Note: Disconnect akan di-handle oleh caller untuk memastikan koneksi tetap hidup jika diperlukan
            # Tapi untuk safety, kita tidak disconnect di sini karena caller yang bertanggung jawab
            pass
    
    async def get_session_info(self):
        """Mendapatkan informasi session"""
        try:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    return None, "Tidak dapat terhubung"
            
            me = await self.get_me()
            if me:
                return {
                    'phone': self.phone_number,
                    'name': f"{me.first_name or ''} {me.last_name or ''}".strip(),
                    'username': me.username or 'Tidak ada',
                    'id': me.id,
                    'session_path': self.session_path
                }, None
            return None, "Tidak dapat mendapatkan info user"
        except Exception as e:
            return None, f"Error: {str(e)}"
        # Note: Disconnect akan di-handle oleh caller

    async def get_contacts_mutual_stats(self, exclude_bots: bool = True):
        """Statistik kontak di buku telepon: mutual vs non-mutual (flag dari Telegram)."""
        try:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    return None, "Tidak dapat terhubung"

            result = await self.client(GetContactsRequest(hash=0))
            users_by_id = {u.id: u for u in result.users if isinstance(u, User)}

            mutual = 0
            non_mutual = 0
            for c in result.contacts:
                uid = c.user_id
                u = users_by_id.get(uid)
                if exclude_bots and u and getattr(u, "bot", False):
                    continue
                if getattr(c, "mutual", False):
                    mutual += 1
                else:
                    non_mutual += 1

            return {
                "mutual": mutual,
                "non_mutual": non_mutual,
                "total": mutual + non_mutual,
            }, None
        except Exception as e:
            return None, f"Error: {str(e)}"

    async def export_string_session(self):
        """Export session sebagai string session"""
        try:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    return None, "Tidak dapat terhubung ke Telegram"
            
            # Export session sebagai string
            string_session = self.client.session.save()
            return string_session, None
        except Exception as e:
            return None, f"Error: {str(e)}"
    
    async def join_channel_if_needed(self, channel_username: str):
        """Join channel jika diperlukan (untuk private channel)"""
        try:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    return False, "Tidak dapat terhubung ke Telegram"
            
            try:
                # Coba dapatkan entity channel
                entity = await self.client.get_entity(channel_username)
                
                # Cek apakah bisa akses pesan
                try:
                    # Coba akses pesan terbaru
                    await self.client.get_messages(entity, limit=1)
                    return True, None  # Sudah bisa akses, tidak perlu join
                except Exception as access_error:
                    # Jika tidak bisa akses, coba join channel (untuk private channel)
                    try:
                        await self.client(JoinChannelRequest(entity))
                        # Tunggu sebentar setelah join
                        await asyncio.sleep(0.5)
                        # Coba lagi akses pesan
                        await self.client.get_messages(entity, limit=1)
                        return True, None
                    except Exception as join_error:
                        # Jika masih error, mungkin channel public tapi ada masalah lain
                        # Atau channel memerlukan approval untuk join
                        error_msg = str(join_error).lower()
                        if "already" in error_msg or "participant" in error_msg:
                            # Sudah join atau sudah participant, coba lagi akses
                            try:
                                await self.client.get_messages(entity, limit=1)
                                return True, None
                            except:
                                return False, "Tidak dapat mengakses channel setelah join"
                        return False, f"Tidak dapat join channel: {str(join_error)}"
            except Exception as e:
                error_msg = str(e)
                if "username" in error_msg.lower() or "not found" in error_msg.lower():
                    return False, "Channel tidak ditemukan"
                elif "flood" in error_msg.lower():
                    return False, "Rate limit: Terlalu banyak request"
                return False, f"Error: {error_msg}"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    async def get_latest_post(self, channel_username: str):
        """Mendapatkan postingan terbaru dari channel (hanya post asli, bukan service message)"""
        try:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    return None, "Tidak dapat terhubung ke Telegram"
            
            # Coba join channel jika diperlukan
            join_success, join_error = await self.join_channel_if_needed(channel_username)
            if not join_success:
                return None, join_error or "Tidak dapat mengakses channel"
            
            try:
                # Ambil beberapa pesan terbaru, lalu filter yang bukan service message
                # limit=10 untuk antisipasi jika ada banyak service messages berturut-turut
                messages = await self.client.get_messages(channel_username, limit=10)
                if messages and len(messages) > 0:
                    for msg in messages:
                        # Skip service messages (pin, join, leave, foto channel, dll)
                        if isinstance(msg, MessageService):
                            continue
                        # Pastikan ini pesan channel yang sesungguhnya (punya text, media, atau document)
                        if msg.message is not None or msg.media is not None:
                            return msg, None
                    # Semua 10 pesan terakhir adalah service messages
                    return None, "Tidak ada postingan konten ditemukan (hanya service messages)"
                else:
                    return None, "Tidak ada postingan ditemukan di channel"
            except Exception as e:
                error_msg = str(e)
                if "username" in error_msg.lower() or "not found" in error_msg.lower():
                    return None, "Channel tidak ditemukan atau tidak bisa diakses"
                elif "flood" in error_msg.lower():
                    return None, "Rate limit: Terlalu banyak request"
                return None, f"Error: {error_msg}"
        except Exception as e:
            return None, f"Error: {str(e)}"
    
    async def get_new_posts_since(self, channel_username: str, since_post_id: int):
        """Mendapatkan semua post baru setelah post_id tertentu (hanya post asli)
        
        Returns:
            list: Daftar post baru (dari terlama ke terbaru), atau [] jika tidak ada
            str: Error message jika ada error, atau None
        """
        try:
            if not self.is_connected:
                connected = await self.connect()
                if not connected:
                    return [], "Tidak dapat terhubung ke Telegram"
            
            try:
                # Ambil pesan terbaru, filter yang baru (ID > since_post_id)
                messages = await self.client.get_messages(channel_username, limit=20)
                new_posts = []
                if messages:
                    for msg in messages:
                        # Skip service messages
                        if isinstance(msg, MessageService):
                            continue
                        # Hanya ambil yang lebih baru dari baseline
                        if msg.id <= since_post_id:
                            break  # Sudah sampai post yang sudah diketahui
                        # Pastikan ini pesan konten (bukan kosong)
                        if msg.message is not None or msg.media is not None:
                            new_posts.append(msg)
                
                # Return dari terlama ke terbaru agar di-boost berurutan
                new_posts.reverse()
                return new_posts, None
            except Exception as e:
                error_msg = str(e)
                if "username" in error_msg.lower() or "not found" in error_msg.lower():
                    return [], "Channel tidak ditemukan atau tidak bisa diakses"
                elif "flood" in error_msg.lower():
                    return [], "Rate limit: Terlalu banyak request"
                return [], f"Error: {error_msg}"
        except Exception as e:
            return [], f"Error: {str(e)}"
    
    async def view_post(self, channel_username: str, post_id: int = None):
        """Melihat postingan di channel untuk menaikkan views"""
        try:
            # Cek koneksi
            if not self.is_connected:
                try:
                    connected = await self.connect()
                    if not connected:
                        return False, "Tidak dapat terhubung ke Telegram"
                except Exception as e:
                    error_msg = str(e).lower()
                    if "unauthorized" in error_msg or "auth" in error_msg:
                        return False, "Session tidak valid (unauthorized)"
                    elif "timeout" in error_msg or "connection" in error_msg:
                        return False, "Timeout: Gagal terhubung ke Telegram"
                    else:
                        return False, f"Error koneksi: {str(e)}"
            
            # PENTING: Selalu coba join channel dulu sebelum view
            # Ini diperlukan untuk channel yang memerlukan join
            try:
                join_success, join_error = await self.join_channel_if_needed(channel_username)
                if not join_success:
                    # Jika gagal join, tetap coba lanjut (mungkin public channel)
                    pass
            except Exception as join_exc:
                # Ignore join error, tetap coba view
                pass
            
            # Jika post_id tidak diberikan, ambil post terbaru
            if post_id is None:
                try:
                    latest_post, error = await self.get_latest_post(channel_username)
                    if error:
                        return False, error
                    if not latest_post:
                        return False, "Tidak ada postingan ditemukan"
                    post_id = latest_post.id
                except Exception as e:
                    error_msg = str(e).lower()
                    if "username" in error_msg or "not found" in error_msg:
                        return False, "Channel tidak ditemukan"
                    elif "flood" in error_msg:
                        return False, "Rate limit: Terlalu banyak request"
                    else:
                        return False, f"Error mengambil post: {str(e)}"
            
            # Buka postingan (view post)
            try:
                # Dapatkan entity channel dengan retry
                entity = None
                max_retries = 2
                for attempt in range(max_retries):
                    try:
                        entity = await self.client.get_entity(channel_username)
                        break
                    except Exception as e:
                        if attempt == max_retries - 1:
                            error_msg = str(e).lower()
                            if "username" in error_msg or "not found" in error_msg:
                                return False, "Channel tidak ditemukan atau tidak bisa diakses"
                            elif "flood" in error_msg:
                                return False, "Rate limit: Terlalu banyak request"
                            else:
                                return False, f"Error mendapatkan channel: {str(e)}"
                        await asyncio.sleep(1)  # Retry delay
                
                if not entity:
                    return False, "Gagal mendapatkan entity channel"
                
                # Method 1: Get message dengan ids untuk trigger view counter
                message = None
                try:
                    message = await self.client.get_messages(entity, ids=post_id)
                except Exception as e:
                    error_msg = str(e).lower()
                    if "message not found" in error_msg or "not found" in error_msg:
                        return False, f"Post ID {post_id} tidak ditemukan"
                    elif "flood" in error_msg:
                        return False, "Rate limit: Terlalu banyak request"
                    elif "unauthorized" in error_msg:
                        return False, "Tidak memiliki akses ke channel"
                    else:
                        return False, f"Error mengambil pesan: {str(e)}"
                
                if not message:
                    return False, f"Post ID {post_id} tidak ditemukan"
                
                # Method utama: Gunakan GetMessagesViewsRequest dengan increment=True
                # Ini adalah cara yang paling efektif untuk menaikkan views
                try:
                    views_result = await self.client(GetMessagesViewsRequest(
                        peer=entity,
                        id=[post_id],
                        increment=True
                    ))
                    
                    # Jika berhasil, views sudah dinaikkan
                    if views_result and len(views_result.views) > 0:
                        views_count = views_result.views[0].views if hasattr(views_result.views[0], 'views') else None
                        
                        # Method tambahan: Coba akses media jika ada (untuk trigger view yang lebih efektif)
                        if message.media:
                            try:
                                # Download media thumbnail/preview untuk trigger view tambahan
                                await self.client.download_media(message.media, file=bytes, in_memory=True)
                            except Exception as media_error:
                                # Jika gagal download media, tidak masalah, lanjutkan
                                error_msg = str(media_error).lower()
                                if "flood" in error_msg:
                                    # Jika rate limit saat download media, skip saja
                                    pass
                                # Continue dengan proses view
                        
                        # Tambahkan delay untuk terlihat natural
                        try:
                            await asyncio.sleep(1.5)
                        except Exception:
                            pass  # Ignore jika sleep diinterrupt
                        
                        return True, f"Post ID {post_id} berhasil di-view"
                    else:
                        # Jika views_result kosong, coba fallback method
                        return False, "Gagal mendapatkan views result"
                        
                except Exception as views_error:
                    # Jika GetMessagesViewsRequest gagal, gunakan fallback method
                    error_msg = str(views_error).lower()
                    
                    # Jika error spesifik, return error
                    if "flood" in error_msg or "rate limit" in error_msg:
                        return False, "Rate limit: Terlalu banyak request"
                    elif "unauthorized" in error_msg:
                        return False, "Tidak memiliki akses ke channel (unauthorized)"
                    elif "message not found" in error_msg:
                        return False, f"Post ID {post_id} tidak ditemukan"
                    
                    # Fallback: Gunakan method lama (get_messages) jika GetMessagesViewsRequest gagal
                    try:
                        # Coba akses media jika ada
                        if message.media:
                            try:
                                await self.client.download_media(message.media, file=bytes, in_memory=True)
                            except Exception:
                                pass
                        
                        # Delay untuk terlihat natural
                        await asyncio.sleep(1.5)
                        
                        # Get message sekali lagi sebagai fallback
                        try:
                            await self.client.get_messages(entity, ids=post_id)
                        except Exception:
                            pass
                        
                        # Return dengan warning bahwa menggunakan fallback
                        return True, f"Post ID {post_id} di-view (fallback method)"
                    except Exception as fallback_error:
                        return False, f"Error: {str(fallback_error)}"
                
            except Exception as e:
                error_msg = str(e).lower()
                # Kategorikan error untuk pesan yang lebih informatif
                if "username" in error_msg or "not found" in error_msg:
                    return False, "Channel tidak ditemukan atau tidak bisa diakses"
                elif "flood" in error_msg or "rate limit" in error_msg:
                    return False, "Rate limit: Terlalu banyak request, coba lagi nanti"
                elif "unauthorized" in error_msg or "auth" in error_msg:
                    return False, "Tidak memiliki akses ke channel (unauthorized)"
                elif "timeout" in error_msg or "connection" in error_msg:
                    return False, "Timeout: Gagal terhubung ke Telegram"
                elif "banned" in error_msg or "blocked" in error_msg:
                    return False, "Akun diblokir atau dibanned"
                else:
                    return False, f"Error: {str(e)}"
        except Exception as e:
            # Catch-all untuk error yang tidak terduga
            error_msg = str(e).lower()
            if "unauthorized" in error_msg:
                return False, "Session tidak valid (unauthorized)"
            elif "timeout" in error_msg:
                return False, "Timeout: Gagal terhubung"
            else:
                return False, f"Error tidak terduga: {str(e)}"

    async def resolve_group_entity_for_invite(self, group_link: str):
        """Resolve grup/supergroup dari link undangan atau username publik."""
        link = (group_link or "").strip()
        if not link:
            return None, "Link kosong."

        m = re.search(r'(?:telegram\.me|t\.me)/\+([A-Za-z0-9_-]+)', link, re.I)
        if not m:
            m = re.search(r'joinchat/([A-Za-z0-9_-]+)', link, re.I)
        if m:
            inv_hash = m.group(1)
            try:
                checked = await self.client(CheckChatInviteRequest(inv_hash))
                if isinstance(checked, ChatInviteAlready):
                    ch = getattr(checked, "chat", None)
                    if ch is not None:
                        return ch, None
                updates = await self.client(ImportChatInviteRequest(inv_hash))
                chats = getattr(updates, "chats", None) or []
                if chats:
                    return chats[0], None
                return None, "Undangan tidak mengembalikan info grup (kedaluwarsa atau tidak valid?)."
            except Exception as e:
                err_low = str(e).lower()
                if (
                    "already a participant" in err_low
                    or "already_participant" in err_low
                    or "useralreadyparticipant" in err_low
                ):
                    try:
                        checked = await self.client(CheckChatInviteRequest(inv_hash))
                        if isinstance(checked, ChatInviteAlready):
                            ch = getattr(checked, "chat", None)
                            if ch is not None:
                                return ch, None
                    except Exception:
                        pass
                return None, f"Gagal buka undangan: {str(e)}"

        m = re.search(r'(?:telegram\.me|t\.me)/([a-zA-Z_][a-zA-Z0-9_]{3,})', link, re.I)
        uname = m.group(1) if m else None
        if uname and uname.lower() in (
            'addstickers', 'share', 'iv', 'login', 'joinchat',
        ):
            uname = None
        if uname is None and link.startswith('@'):
            cand = link[1:].strip()
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]{3,}$', cand):
                uname = cand
        if uname is None and re.match(r'^[a-zA-Z_][a-zA-Z0-9_]{3,}$', link):
            uname = link

        if uname:
            try:
                ent = await self.client.get_entity(uname)
                if isinstance(ent, User):
                    return None, "Bukan grup/channel (ini pengguna)."
                return ent, None
            except Exception as e:
                return None, f"Gagal membuka grup: {str(e)}"

        return None, (
            "Format tidak dikenali.\n"
            "Gunakan link `https://t.me/+hashUndangan` atau `@usernameGrup` / URL publik."
        )

    async def _ensure_session_joined_target_group(self, entity):
        """Pastikan akun session sudah bergabung ke grup tujuan sebelum mengundang kontak.

        Alur:
        1) Channel/supergroup: cek keanggotaan (GetParticipantRequest) → jika belum,
           JoinChannelRequest (retry jika FloodWait) → verifikasi lagi / fallback get_messages.
        2) Grup klasik (Chat): undangan ImportChatInvite biasanya sudah menambahkan akun;
           jika belum anggota, akses pesan gagal → minta pakai link undangan.
        """
        if isinstance(entity, Channel):
            try:
                await self.client(
                    GetParticipantRequest(channel=entity, participant=InputPeerSelf())
                )
                return True, None
            except UserNotParticipantError:
                pass
            except Exception:
                # Mis. channel sembunyikan daftar anggota — lanjut coba join
                pass

            last_err = None
            for attempt in range(3):
                try:
                    await self.client(JoinChannelRequest(entity))
                    try:
                        await self.client(
                            GetParticipantRequest(
                                channel=entity, participant=InputPeerSelf()
                            )
                        )
                    except Exception:
                        pass
                    return True, None
                except UserAlreadyParticipantError:
                    return True, None
                except FloodWaitError as e:
                    last_err = e
                    wait = min(int(e.seconds) + 1, 120)
                    await asyncio.sleep(wait)
                    continue
                except Exception as e:
                    last_err = e
                    lowered = str(e).lower()
                    if "already" in lowered or "participant" in lowered:
                        return True, None
                    if attempt < 2:
                        await asyncio.sleep(1.5)
                        continue
                    break

            try:
                await self.client(
                    GetParticipantRequest(channel=entity, participant=InputPeerSelf())
                )
                return True, None
            except Exception:
                pass
            try:
                await self.client.get_messages(entity, limit=1)
                return True, None
            except Exception:
                return False, (
                    str(last_err)
                    if last_err
                    else "Akun tidak bisa bergabung atau memverifikasi keanggotaan di channel ini."
                )

        if isinstance(entity, Chat):
            try:
                await self.client.get_messages(entity, limit=1)
                return True, None
            except UserNotParticipantError:
                return (
                    False,
                    "Akun session belum menjadi anggota grup ini. "
                    "Kirim link undangan (https://t.me/+…) supaya akun bergabung dulu, lalu ulangi.",
                )
            except Exception as e:
                el = str(e).lower()
                if (
                    "participant" in el
                    or "not a member" in el
                    or "chat_write" in el
                    or "invite" in el
                ):
                    return (
                        False,
                        "Akun session belum di grup. Gunakan link undangan agar akun bergabung terlebih dahulu.",
                    )
                return False, str(e)

        return True, None

    async def join_group_with_link(self, group_link: str):
        """Gabung ke grup/supergroup dari link undangan atau @username / URL publik."""
        if not self.is_connected:
            return {
                "ok": False,
                "error": "Belum terhubung ke Telegram.",
                "group_label": None,
                "already_member": False,
            }

        entity, err = await self.resolve_group_entity_for_invite(group_link)
        if err:
            return {
                "ok": False,
                "error": err,
                "group_label": None,
                "already_member": False,
            }

        group_label = (
            getattr(entity, "title", None)
            or getattr(entity, "username", "")
            or "grup"
        )

        already_member = False
        if isinstance(entity, Channel):
            try:
                await self.client(
                    GetParticipantRequest(channel=entity, participant=InputPeerSelf())
                )
                already_member = True
            except UserNotParticipantError:
                pass
            except Exception:
                pass
        elif isinstance(entity, Chat):
            try:
                await self.client.get_messages(entity, limit=1)
                already_member = True
            except UserNotParticipantError:
                pass
            except Exception:
                pass

        ok_join, join_err = await self._ensure_session_joined_target_group(entity)
        if not ok_join:
            return {
                "ok": False,
                "error": join_err or "Gagal bergabung ke grup.",
                "group_label": group_label,
                "already_member": False,
            }

        return {
            "ok": True,
            "error": None,
            "group_label": group_label,
            "already_member": already_member,
        }

    def _split_contacts_mutual_non_mutual(self, contacts_res, me_id: int):
        """Dari hasil GetContactsRequest: dua list User (mutual dulu, non-mutual), tanpa bot & diri sendiri."""
        users_by_id = {u.id: u for u in contacts_res.users if isinstance(u, User)}
        mutual_users = []
        non_mutual_users = []
        for c in contacts_res.contacts:
            uid = c.user_id
            u = users_by_id.get(uid)
            if not u or getattr(u, "bot", False) or u.id == me_id:
                continue
            if getattr(c, "mutual", False):
                mutual_users.append(u)
            else:
                non_mutual_users.append(u)
        return mutual_users, non_mutual_users

    def _partition_users_by_contact_mutual(self, contacts_res, me_id: int, user_list):
        """Bagi list User menjadi mutual vs selainnya menurut flag mutual di buku kontak."""
        contact_mutual_ids = set()
        for c in contacts_res.contacts:
            if c.user_id == me_id:
                continue
            if getattr(c, "mutual", False):
                contact_mutual_ids.add(c.user_id)
        mutual_users = []
        non_mutual_users = []
        seen = set()
        for u in user_list:
            if not isinstance(u, User):
                continue
            if u.id in seen:
                continue
            seen.add(u.id)
            if getattr(u, "bot", False) or u.id == me_id:
                continue
            if u.id in contact_mutual_ids:
                mutual_users.append(u)
            else:
                non_mutual_users.append(u)
        return mutual_users, non_mutual_users

    @staticmethod
    def _same_tg_group(a, b) -> bool:
        if isinstance(a, Channel) and isinstance(b, Channel):
            return a.id == b.id
        if isinstance(a, Chat) and isinstance(b, Chat):
            return a.id == b.id
        return False

    @staticmethod
    def user_matches_last_seen_days(user: User, max_days: int) -> bool:
        """True jika aktivitas/last seen dianggap dalam rentang max_days (kalender hari, UTC)."""
        st = getattr(user, "status", None)
        if st is None or isinstance(st, UserStatusEmpty):
            return False
        now = datetime.now(timezone.utc)
        if isinstance(st, UserStatusOnline):
            return True
        if isinstance(st, UserStatusRecently):
            return max_days >= 1
        if isinstance(st, UserStatusLastWeek):
            return max_days >= 7
        if isinstance(st, UserStatusLastMonth):
            return max_days >= 30
        if isinstance(st, UserStatusOffline):
            wo = st.was_online
            if wo is None:
                return False
            if wo.tzinfo is None:
                wo = wo.replace(tzinfo=timezone.utc)
            return (now - wo) <= timedelta(days=max_days)
        return False

    async def _collect_user_pool(
        self,
        source_entity,
        participants_limit: int = 0,
        message_fallback_limit: int = HIDDEN_MEMBERS_MSG_FALLBACK_LIMIT,
    ):
        """Kumpulkan dict {uid: User} dari grup sumber, dengan fallback otomatis.

        Strategi:
        1. Coba `iter_participants` (limit=`participants_limit`; 0 = semua).
        2. Jika Telegram tolak (`ChatAdminRequiredError` / member tersembunyi),
           fallback: baca `iter_messages(limit=message_fallback_limit)` dan
           kumpulkan pengirim unik. Tidak butuh admin & tidak butuh akses list peserta.

        Returns:
            (users_by_id: dict, mode: str, note: str|None)
            mode = 'participants' | 'messages_fallback' | 'mixed'
        """
        users_by_id: dict[int, User] = {}
        mode = "participants"
        note = None
        if not self.is_connected:
            return users_by_id, mode, "client tidak terkoneksi"
        try:
            kwargs = {}
            if participants_limit and participants_limit > 0:
                kwargs["limit"] = participants_limit
            async for p in self.client.iter_participants(source_entity, **kwargs):
                if isinstance(p, User) and not getattr(p, "bot", False):
                    users_by_id[p.id] = p
        except ChatAdminRequiredError:
            mode = "messages_fallback"
            note = "member grup sumber tersembunyi (admin only)"
        except FloodWaitError as fw:
            # Tidak agresif: kembalikan apa adanya + catat.
            note = f"FloodWait iter_participants {int(fw.seconds)}s"
        except Exception as e:
            msg = (str(e) or type(e).__name__).lower()
            if "admin" in msg or "hidden" in msg or "privacy" in msg:
                mode = "messages_fallback"
                note = f"iter_participants ditolak: {str(e)[:80]}"
            else:
                note = f"iter_participants error: {str(e)[:80]}"
        if mode == "messages_fallback" or not users_by_id:
            before = len(users_by_id)
            try:
                async for msg in self.client.iter_messages(
                    source_entity, limit=message_fallback_limit
                ):
                    sender = getattr(msg, "sender", None)
                    if isinstance(sender, User) and not getattr(sender, "bot", False):
                        users_by_id.setdefault(sender.id, sender)
            except Exception as e:
                if note:
                    note = note + f"; iter_messages err: {str(e)[:60]}"
                else:
                    note = f"iter_messages err: {str(e)[:80]}"
            else:
                if before > 0:
                    mode = "mixed"
        return users_by_id, mode, note

    async def collect_scraped_users_filtered(self, source_entity, last_seen_days: int):
        """Daftar User di grup sumber yang lolos filter last seen.

        Mengembalikan **list[User]** (kompatibel dengan pemakai lama). Untuk
        info mode/note, gunakan ``collect_scraped_users_filtered_v2``.
        """
        users, _mode, _note = await self.collect_scraped_users_filtered_v2(
            source_entity, last_seen_days
        )
        return users

    async def collect_scraped_users_filtered_v2(
        self, source_entity, last_seen_days: int
    ):
        """Versi v2: kembali (users, mode, note) dengan fallback ke iter_messages."""
        if not self.is_connected:
            return [], "participants", "client tidak terkoneksi"
        try:
            me = await self.client.get_me()
            me_id = me.id if me else 0
        except Exception:
            me_id = 0
        users_by_id, mode, note = await self._collect_user_pool(source_entity)
        out = []
        for u in users_by_id.values():
            if not isinstance(u, User) or getattr(u, "bot", False) or u.id == me_id:
                continue
            if not self.user_matches_last_seen_days(u, last_seen_days):
                continue
            out.append(u)
        return out, mode, note

    async def _hydrate_users_for_invite(self, users: list, failed_sample: list, max_failed: int = 14):
        """Entity User dari session lain punya access_hash milik client lain; ambil ulang via client ini."""
        if not self.is_connected or not users:
            return []
        try:
            me = await self.client.get_me()
            me_id = me.id if me else 0
        except Exception:
            me_id = 0
        out = []
        for u in users:
            if not isinstance(u, User):
                continue
            uid = getattr(u, "id", None)
            if uid is None or uid == me_id or getattr(u, "bot", False):
                continue
            try:
                fresh = await self.client.get_entity(uid)
                if isinstance(fresh, User) and not getattr(fresh, "bot", False) and fresh.id != me_id:
                    out.append(fresh)
            except Exception as e:
                if len(failed_sample) < max_failed:
                    failed_sample.append((uid, (str(e) or "gagal resolve user")[:120]))
        return out

    async def invite_user_chunk_to_target(
        self,
        target_entity,
        users: list,
        failed_sample: list = None,
        skip_hydrate: bool = False,
    ):
        """Satu putaran undangan: list User ke grup tujuan. Return invited, flood_wait_seconds, remaining_users.

        skip_hydrate=True: dipakai pemanggil yang sudah memastikan setiap User
        adalah objek **valid untuk client ini** (mis. dari iter_participants
        client yang sama). Menghindari `get_entity` yang gagal karena peer cache kosong.
        """
        if failed_sample is None:
            failed_sample = []
        if not self.is_connected or not users:
            return {
                "invited": 0,
                "flood_wait_seconds": None,
                "failed_sample": failed_sample,
                "remaining_users": [],
            }
        if skip_hydrate:
            hydrated = [u for u in users if isinstance(u, User)]
        else:
            original_users = list(users)
            hydrated = await self._hydrate_users_for_invite(users, failed_sample)
            if not hydrated:
                return {
                    "invited": 0,
                    "flood_wait_seconds": None,
                    "failed_sample": failed_sample,
                    "remaining_users": original_users,
                }
        if not hydrated:
            return {
                "invited": 0,
                "flood_wait_seconds": None,
                "failed_sample": failed_sample,
                "remaining_users": [],
            }
        n = len(hydrated)
        if isinstance(target_entity, Channel):
            sub = await self._invite_users_to_channel_batches(
                target_entity,
                hydrated,
                n,
                2.0,
                failed_sample,
                inter_contact_delay=NON_MUTUAL_INVITE_DELAY_SEC,
            )
        elif isinstance(target_entity, Chat):
            sub = await self._invite_users_to_basic_group(
                target_entity,
                hydrated,
                failed_sample,
                inter_contact_delay=NON_MUTUAL_INVITE_DELAY_SEC,
            )
        else:
            return {
                "invited": 0,
                "flood_wait_seconds": None,
                "failed_sample": failed_sample,
                "remaining_users": [],
            }
        sub["failed_sample"] = failed_sample
        sub.setdefault("remaining_users", [])
        return sub

    async def _apply_flood_wait_invite_policy(self, e: FloodWaitError):
        """FloodWait > 5 menit → return detik (hentikan). Selain itu tunggu lalu return None (ulangi request)."""
        sec = int(getattr(e, "seconds", 0) or 0)
        if sec > FLOOD_WAIT_ABORT_ABOVE_SEC:
            return sec
        await asyncio.sleep(sec + 1)
        return None

    async def _invite_users_to_channel_batches(
        self,
        entity,
        users,
        batch_size,
        batch_delay,
        failed_sample,
        max_failed_samples: int = 8,
        inter_contact_delay: float = 0.0,
    ):
        """Undang users ke channel. inter_contact_delay: jeda setelah tiap kontak (untuk non-mutual)."""
        async def record_fail(uid, msg):
            if len(failed_sample) < max_failed_samples:
                failed_sample.append((uid, (msg or "")[:120]))

        invited = 0
        i = 0
        n = len(users)
        while i < n:
            batch = users[i : i + batch_size]
            batch_confirmed = False
            while not batch_confirmed:
                try:
                    res = await self.client(
                        InviteToChannelRequest(channel=entity, users=batch)
                    )
                    invited += _count_invited_users_from_invite_updates(res, batch)
                    batch_confirmed = True
                    if inter_contact_delay > 0:
                        await asyncio.sleep(inter_contact_delay)
                except FloodWaitError as e:
                    stop_sec = await self._apply_flood_wait_invite_policy(e)
                    if stop_sec is not None:
                        return {
                            "invited": invited,
                            "flood_wait_seconds": stop_sec,
                            "remaining_users": list(users[i:]),
                        }
                except PeerFloodError:
                    # Akun ditandai anti-spam oleh Telegram. Berhenti total untuk
                    # session ini agar pemanggil mengeluarkannya dari pool aktif.
                    return {
                        "invited": invited,
                        "flood_wait_seconds": PEER_FLOOD_COOLDOWN_SEC,
                        "remaining_users": list(users[i:]),
                        "peer_flood": True,
                    }
                except TypeError as te:
                    if _is_updates_parsing_bug(te):
                        # Request kemungkinan besar sukses; Telethon hanya gagal
                        # mem-parse respons. Hitung sebagai berhasil agar batch
                        # tidak diulang.
                        invited += len(batch)
                        batch_confirmed = True
                        if inter_contact_delay > 0:
                            await asyncio.sleep(inter_contact_delay)
                        continue
                    # TypeError lain → jatuh ke per-user fallback.
                    for j, u in enumerate(batch):
                        user_done = False
                        while not user_done:
                            try:
                                res = await self.client(
                                    InviteToChannelRequest(channel=entity, users=[u])
                                )
                                invited += _count_invited_users_from_invite_updates(res, [u])
                                user_done = True
                            except TypeError as te2:
                                if _is_updates_parsing_bug(te2):
                                    invited += 1
                                    user_done = True
                                else:
                                    await record_fail(u.id, str(te2))
                                    user_done = True
                            except Exception as ee:
                                await record_fail(u.id, str(ee))
                                user_done = True
                            delay = (
                                inter_contact_delay
                                if inter_contact_delay > 0
                                else 0.15
                            )
                            await asyncio.sleep(delay)
                    batch_confirmed = True
                except Exception:
                    for j, u in enumerate(batch):
                        user_done = False
                        while not user_done:
                            try:
                                res = await self.client(
                                    InviteToChannelRequest(channel=entity, users=[u])
                                )
                                invited += _count_invited_users_from_invite_updates(res, [u])
                                user_done = True
                                delay = (
                                    inter_contact_delay
                                    if inter_contact_delay > 0
                                    else 0.15
                                )
                                await asyncio.sleep(delay)
                            except FloodWaitError as fe:
                                stop_sec = await self._apply_flood_wait_invite_policy(fe)
                                if stop_sec is not None:
                                    tail = batch[j:] if j < len(batch) else []
                                    return {
                                        "invited": invited,
                                        "flood_wait_seconds": stop_sec,
                                        "remaining_users": list(tail)
                                        + list(users[i + len(batch) :]),
                                    }
                            except PeerFloodError:
                                tail = batch[j:] if j < len(batch) else []
                                return {
                                    "invited": invited,
                                    "flood_wait_seconds": PEER_FLOOD_COOLDOWN_SEC,
                                    "remaining_users": list(tail)
                                    + list(users[i + len(batch) :]),
                                    "peer_flood": True,
                                }
                            except UserAlreadyParticipantError:
                                # Sudah ada di grup → anggap "sudah masuk".
                                invited += 1
                                user_done = True
                                delay = (
                                    inter_contact_delay
                                    if inter_contact_delay > 0
                                    else 0.15
                                )
                                await asyncio.sleep(delay)
                            except TypeError as te3:
                                if _is_updates_parsing_bug(te3):
                                    invited += 1
                                    user_done = True
                                    delay = (
                                        inter_contact_delay
                                        if inter_contact_delay > 0
                                        else 0.15
                                    )
                                    await asyncio.sleep(delay)
                                    continue
                                await record_fail(u.id, str(te3))
                                delay = (
                                    inter_contact_delay
                                    if inter_contact_delay > 0
                                    else 0.15
                                )
                                await asyncio.sleep(delay)
                                user_done = True
                            except Exception as ee:
                                el = str(ee).lower()
                                if (
                                    "already" in el and "participant" in el
                                ) or "useralreadyparticipant" in el:
                                    invited += 1
                                    user_done = True
                                    delay = (
                                        inter_contact_delay
                                        if inter_contact_delay > 0
                                        else 0.15
                                    )
                                    await asyncio.sleep(delay)
                                    continue
                                # "Too many requests" tanpa class PeerFloodError
                                # (mis. RPCError generic dari Telegram) → tetap
                                # diperlakukan sebagai anti-spam → hentikan akun.
                                if "too many requests" in el or "peer_flood" in el:
                                    tail = batch[j:] if j < len(batch) else []
                                    return {
                                        "invited": invited,
                                        "flood_wait_seconds": PEER_FLOOD_COOLDOWN_SEC,
                                        "remaining_users": list(tail)
                                        + list(users[i + len(batch) :]),
                                        "peer_flood": True,
                                    }
                                await record_fail(u.id, str(ee))
                                delay = (
                                    inter_contact_delay
                                    if inter_contact_delay > 0
                                    else 0.15
                                )
                                await asyncio.sleep(delay)
                                user_done = True
                    batch_confirmed = True
            i += len(batch)
            await asyncio.sleep(batch_delay)
        return {"invited": invited, "flood_wait_seconds": None, "remaining_users": []}

    async def _invite_users_to_basic_group(
        self,
        entity,
        users,
        failed_sample,
        max_failed_samples: int = 8,
        inter_contact_delay: float = 0.35,
    ):
        """AddChatUserRequest satu per satu."""
        async def record_fail(uid, msg):
            if len(failed_sample) < max_failed_samples:
                failed_sample.append((uid, (msg or "")[:120]))

        invited = 0
        for idx_u, u in enumerate(users):
            user_done = False
            while not user_done:
                try:
                    inp = InputUser(user_id=u.id, access_hash=u.access_hash or 0)
                    await self.client(
                        AddChatUserRequest(
                            chat_id=entity.id,
                            user_id=inp,
                            fwd_limit=0,
                        )
                    )
                    invited += 1
                    user_done = True
                except FloodWaitError as e:
                    stop_sec = await self._apply_flood_wait_invite_policy(e)
                    if stop_sec is not None:
                        return {
                            "invited": invited,
                            "flood_wait_seconds": stop_sec,
                            "remaining_users": list(users[idx_u:]),
                        }
                except PeerFloodError:
                    return {
                        "invited": invited,
                        "flood_wait_seconds": PEER_FLOOD_COOLDOWN_SEC,
                        "remaining_users": list(users[idx_u:]),
                        "peer_flood": True,
                    }
                except UserAlreadyParticipantError:
                    invited += 1
                    user_done = True
                except TypeError as te:
                    if _is_updates_parsing_bug(te):
                        # Telethon parsing bug — request kemungkinan sukses.
                        invited += 1
                        user_done = True
                    else:
                        await record_fail(u.id, str(te))
                        user_done = True
                except Exception as ee:
                    el = str(ee).lower()
                    if "too many requests" in el or "peer_flood" in el:
                        return {
                            "invited": invited,
                            "flood_wait_seconds": PEER_FLOOD_COOLDOWN_SEC,
                            "remaining_users": list(users[idx_u:]),
                            "peer_flood": True,
                        }
                    await record_fail(u.id, str(ee))
                    user_done = True
            await asyncio.sleep(inter_contact_delay)
        return {"invited": invited, "flood_wait_seconds": None, "remaining_users": []}

    def _result_flood_stopped(
        self,
        group_label,
        mutual_users,
        non_mutual_users,
        invited_mutual,
        invited_non_mutual,
        flood_seconds,
        failed_sample,
    ):
        """Response standar saat FloodWait menghentikan undangan."""
        total_scheduled = len(mutual_users) + len(non_mutual_users)
        invited = invited_mutual + invited_non_mutual
        return {
            "ok": False,
            "stopped_by_flood": True,
            "flood_wait_seconds": flood_seconds,
            "error": (
                f"Undangan dihentikan: FloodWait {flood_seconds} d "
                f"(lebih dari {FLOOD_WAIT_ABORT_ABOVE_SEC // 60} menit)."
            ),
            "group_label": group_label,
            "total_contacts": total_scheduled,
            "mutual_total": len(mutual_users),
            "non_mutual_total": len(non_mutual_users),
            "invited": invited,
            "invited_mutual": invited_mutual,
            "invited_non_mutual": invited_non_mutual,
            "failed_sample": failed_sample,
            "failed": max(0, total_scheduled - invited),
        }

    def _empty_scrape_invite_result(self, error_msg: str, src_label=None, tgt_label=None):
        return {
            "ok": False,
            "error": error_msg,
            "total_contacts": 0,
            "invited": 0,
            "failed": 0,
            "failed_sample": [],
            "source_group_label": src_label,
            "group_label": tgt_label,
        }

    async def list_joined_groups_for_scrape(self):
        """Grup dari dialog akun: grup kecil (Chat) + supergroup (Channel megagroup). Channel siaran diabaikan."""
        if not self.is_connected:
            return []
        items = []
        async for dialog in self.client.iter_dialogs():
            ent = dialog.entity
            if isinstance(ent, User):
                continue
            title = (
                dialog.name
                or getattr(ent, "title", None)
                or getattr(ent, "username", None)
                or "Tanpa nama"
            )
            title = str(title)[:200]
            if isinstance(ent, Chat):
                items.append({"t": "g", "id": ent.id, "title": title})
            elif isinstance(ent, Channel):
                if not getattr(ent, "megagroup", False):
                    continue
                items.append(
                    {
                        "t": "c",
                        "id": ent.id,
                        "ah": ent.access_hash,
                        "title": title,
                    }
                )
        items.sort(key=lambda x: (x.get("title") or "").lower())
        return items

    async def resolve_scrape_peer_entry(self, entry: dict):
        """Resolve entri dict dari list_joined_groups_for_scrape menjadi entity."""
        if not self.is_connected:
            return None, "Belum terhubung ke Telegram."
        try:
            if entry.get("t") == "g":
                return await self.client.get_entity(PeerChat(entry["id"])), None
            if entry.get("t") == "c":
                return await self.client.get_entity(PeerChannel(entry["id"])), None
        except Exception as e:
            return None, str(e)
        return None, "Format grup tidak dikenal."

    async def scrape_group_members_invite_to_target(
        self,
        source_link: str,
        target_link: str,
        batch_size: int = 15,
        batch_delay: float = 2.5,
    ):
        """Baca anggota grup sumber dari link, lalu undang ke grup tujuan."""
        if not self.is_connected:
            return self._empty_scrape_invite_result("Belum terhubung ke Telegram.")

        src_ent, err = await self.resolve_group_entity_for_invite(source_link)
        if err:
            return self._empty_scrape_invite_result(err)

        tgt_ent, err2 = await self.resolve_group_entity_for_invite(target_link)
        if err2:
            return self._empty_scrape_invite_result(err2)

        return await self.scrape_group_members_invite_entities(
            src_ent, tgt_ent, batch_size=batch_size, batch_delay=batch_delay
        )

    async def scrape_group_members_invite_entities(
        self,
        src_ent,
        tgt_ent,
        batch_size: int = 15,
        batch_delay: float = 2.5,
    ):
        """Inti scrape + undang jika sumber dan tujuan sudah berupa entity Telethon."""
        if not self.is_connected:
            return self._empty_scrape_invite_result("Belum terhubung ke Telegram.")

        if self._same_tg_group(src_ent, tgt_ent):
            return self._empty_scrape_invite_result(
                "Grup sumber dan tujuan sama. Gunakan dua grup berbeda."
            )

        src_label = getattr(src_ent, "title", None) or getattr(src_ent, "username", "") or "sumber"
        tgt_label = getattr(tgt_ent, "title", None) or getattr(tgt_ent, "username", "") or "tujuan"

        ok_s, err_s = await self._ensure_session_joined_target_group(src_ent)
        if not ok_s:
            return self._empty_scrape_invite_result(
                f"Tidak bisa mengakses grup sumber: {err_s}", src_label, tgt_label
            )

        ok_t, err_t = await self._ensure_session_joined_target_group(tgt_ent)
        if not ok_t:
            return self._empty_scrape_invite_result(
                f"Tidak bisa memastikan akun di grup tujuan: {err_t}", src_label, tgt_label
            )

        try:
            me = await self.client.get_me()
            scraped = []
            async for p in self.client.iter_participants(src_ent):
                if isinstance(p, User) and not getattr(p, "bot", False) and p.id != me.id:
                    scraped.append(p)
        except Exception as e:
            return self._empty_scrape_invite_result(
                f"Gagal mengambil daftar anggota sumber: {str(e)}", src_label, tgt_label
            )

        if not scraped:
            return {
                "ok": False,
                "error": (
                    "Tidak ada anggota yang terbaca dari grup sumber. "
                    "Mungkin anggota disembunyikan atau akun tidak punya izin melihat partisipan."
                ),
                "total_contacts": 0,
                "invited": 0,
                "failed": 0,
                "failed_sample": [],
                "source_group_label": src_label,
                "group_label": tgt_label,
            }

        try:
            contacts_res = await self.client(GetContactsRequest(hash=0))
            mutual_users, non_mutual_users = self._partition_users_by_contact_mutual(
                contacts_res, me.id, scraped
            )
        except Exception as e:
            return self._empty_scrape_invite_result(
                f"Gagal membaca buku kontak: {str(e)}", src_label, tgt_label
            )

        group_label = tgt_label
        source_group_label = src_label
        failed_sample = []
        invited_mutual = 0
        invited_non_mutual = 0
        sequences = [("mutual", mutual_users), ("non_mutual", non_mutual_users)]

        if isinstance(tgt_ent, Channel):
            for phase, user_list in sequences:
                if not user_list:
                    continue
                if phase == "mutual":
                    sub = await self._invite_users_to_channel_batches(
                        tgt_ent,
                        user_list,
                        batch_size,
                        batch_delay,
                        failed_sample,
                        inter_contact_delay=0.0,
                    )
                else:
                    sub = await self._invite_users_to_channel_batches(
                        tgt_ent,
                        user_list,
                        1,
                        0.0,
                        failed_sample,
                        inter_contact_delay=NON_MUTUAL_INVITE_DELAY_SEC,
                    )
                got = sub["invited"]
                if phase == "mutual":
                    invited_mutual += got
                else:
                    invited_non_mutual += got
                if sub["flood_wait_seconds"] is not None:
                    r = self._result_flood_stopped(
                        group_label,
                        mutual_users,
                        non_mutual_users,
                        invited_mutual,
                        invited_non_mutual,
                        sub["flood_wait_seconds"],
                        failed_sample,
                    )
                    r["source_group_label"] = source_group_label
                    return r

        elif isinstance(tgt_ent, Chat):
            for phase, user_list in sequences:
                if not user_list:
                    continue
                delay_after = 0.35 if phase == "mutual" else NON_MUTUAL_INVITE_DELAY_SEC
                sub = await self._invite_users_to_basic_group(
                    tgt_ent,
                    user_list,
                    failed_sample,
                    inter_contact_delay=delay_after,
                )
                got = sub["invited"]
                if phase == "mutual":
                    invited_mutual += got
                else:
                    invited_non_mutual += got
                if sub["flood_wait_seconds"] is not None:
                    r = self._result_flood_stopped(
                        group_label,
                        mutual_users,
                        non_mutual_users,
                        invited_mutual,
                        invited_non_mutual,
                        sub["flood_wait_seconds"],
                        failed_sample,
                    )
                    r["source_group_label"] = source_group_label
                    return r
        else:
            total_n = len(mutual_users) + len(non_mutual_users)
            return {
                "ok": False,
                "error": "Tipe grup tujuan tidak didukung untuk undangan.",
                "total_contacts": total_n,
                "invited": 0,
                "failed": 0,
                "failed_sample": [],
                "source_group_label": source_group_label,
                "group_label": group_label,
            }

        contact_users_all = mutual_users + non_mutual_users
        invited = invited_mutual + invited_non_mutual
        failed_ct = max(0, len(contact_users_all) - invited)
        return {
            "ok": True,
            "error": None,
            "group_label": group_label,
            "source_group_label": source_group_label,
            "total_contacts": len(contact_users_all),
            "mutual_total": len(mutual_users),
            "non_mutual_total": len(non_mutual_users),
            "invited_mutual": invited_mutual,
            "invited_non_mutual": invited_non_mutual,
            "invited": invited,
            "failed": failed_ct,
            "failed_sample": failed_sample,
        }

    async def invite_contacts_to_group(self, group_link: str, batch_size: int = 15, batch_delay: float = 2.5):
        """Undang semua kontak bukan-bot ke grup. Session bergabung ke grup dulu jika perlu."""
        if not self.is_connected:
            return {
                'ok': False,
                'error': 'Belum terhubung ke Telegram.',
                'total_contacts': 0,
                'invited': 0,
                'failed': 0,
                'failed_sample': [],
            }

        entity, err = await self.resolve_group_entity_for_invite(group_link)
        if err:
            return {
                'ok': False,
                'error': err,
                'total_contacts': 0,
                'invited': 0,
                'failed': 0,
                'failed_sample': [],
            }

        ok_join, join_err = await self._ensure_session_joined_target_group(entity)
        if not ok_join:
            return {
                'ok': False,
                'error': f"Tidak bisa memastikan akun session sudah di grup tujuan: {join_err}",
                'total_contacts': 0,
                'invited': 0,
                'failed': 0,
                'failed_sample': [],
            }

        try:
            me = await self.client.get_me()
            contacts_res = await self.client(GetContactsRequest(hash=0))
            mutual_users, non_mutual_users = self._split_contacts_mutual_non_mutual(
                contacts_res, me.id
            )
        except Exception as e:
            return {
                'ok': False,
                'error': f"Gagal mengambil daftar kontak: {str(e)}",
                'total_contacts': 0,
                'invited': 0,
                'failed': 0,
                'failed_sample': [],
            }

        group_label = getattr(entity, 'title', None) or getattr(entity, 'username', '') or 'grup'
        failed_sample = []
        invited_mutual = 0
        invited_non_mutual = 0

        sequences = [
            ("mutual", mutual_users),
            ("non_mutual", non_mutual_users),
        ]

        if isinstance(entity, Channel):
            for phase, user_list in sequences:
                if not user_list:
                    continue
                if phase == "mutual":
                    sub = await self._invite_users_to_channel_batches(
                        entity,
                        user_list,
                        batch_size,
                        batch_delay,
                        failed_sample,
                        inter_contact_delay=0.0,
                    )
                else:
                    sub = await self._invite_users_to_channel_batches(
                        entity,
                        user_list,
                        1,
                        0.0,
                        failed_sample,
                        inter_contact_delay=NON_MUTUAL_INVITE_DELAY_SEC,
                    )
                got = sub["invited"]
                if phase == "mutual":
                    invited_mutual += got
                else:
                    invited_non_mutual += got
                if sub["flood_wait_seconds"] is not None:
                    return self._result_flood_stopped(
                        group_label,
                        mutual_users,
                        non_mutual_users,
                        invited_mutual,
                        invited_non_mutual,
                        sub["flood_wait_seconds"],
                        failed_sample,
                    )

        elif isinstance(entity, Chat):
            for phase, user_list in sequences:
                if not user_list:
                    continue
                delay_after = (
                    0.35
                    if phase == "mutual"
                    else NON_MUTUAL_INVITE_DELAY_SEC
                )
                sub = await self._invite_users_to_basic_group(
                    entity,
                    user_list,
                    failed_sample,
                    inter_contact_delay=delay_after,
                )
                got = sub["invited"]
                if phase == "mutual":
                    invited_mutual += got
                else:
                    invited_non_mutual += got
                if sub["flood_wait_seconds"] is not None:
                    return self._result_flood_stopped(
                        group_label,
                        mutual_users,
                        non_mutual_users,
                        invited_mutual,
                        invited_non_mutual,
                        sub["flood_wait_seconds"],
                        failed_sample,
                    )
        else:
            total_n = len(mutual_users) + len(non_mutual_users)
            return {
                'ok': False,
                'error': 'Tipe obrolan tidak didukung untuk undangan kontak.',
                'total_contacts': total_n,
                'invited': 0,
                'failed': 0,
                'failed_sample': [],
            }

        contact_users_all = mutual_users + non_mutual_users
        invited = invited_mutual + invited_non_mutual
        failed_ct = max(0, len(contact_users_all) - invited)
        return {
            'ok': True,
            'error': None,
            'group_label': group_label,
            'total_contacts': len(contact_users_all),
            'mutual_total': len(mutual_users),
            'non_mutual_total': len(non_mutual_users),
            'invited_mutual': invited_mutual,
            'invited_non_mutual': invited_non_mutual,
            'invited': invited,
            'failed': failed_ct,
            'failed_sample': failed_sample,
        }

