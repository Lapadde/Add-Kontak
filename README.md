# Telegram Userbot Login Bot

Bot Telegram untuk login akun Telegram menggunakan Telethon dan menyimpan session di `sessions/users/`.

## Fitur

- ✅ Login menggunakan nomor telepon
- ✅ Verifikasi OTP otomatis
- ✅ Support 2FA (Two-Factor Authentication)
- ✅ Penyimpanan session otomatis
- ✅ Validasi input
- ✅ Error handling yang baik

## Persyaratan

- Python 3.8 atau lebih tinggi
- Bot Token dari [@BotFather](https://t.me/BotFather)
- API ID dan API Hash dari [my.telegram.org/apps](https://my.telegram.org/apps)

## Instalasi

1. **Clone atau download project ini**

2. **Install dependencies:**
```bash
pip install -r requirements.txt
```

3. **Buat file `.env` dari `.env.example`:**
```bash
cp .env.example .env
```

4. **Edit file `.env` dan isi dengan kredensial Anda:**
```
BOT_TOKEN=your_bot_token_here
API_ID=your_api_id_here
API_HASH=your_api_hash_here
```

### Cara Mendapatkan Kredensial

#### Bot Token (BOT_TOKEN)
1. Buka Telegram dan cari [@BotFather](https://t.me/BotFather)
2. Kirim command `/newbot`
3. Ikuti instruksi untuk membuat bot baru
4. Salin token yang diberikan

#### API ID dan API Hash
1. Buka [my.telegram.org/apps](https://my.telegram.org/apps)
2. Login dengan nomor telepon Anda
3. Buat aplikasi baru jika belum ada
4. Salin `api_id` dan `api_hash`

## Cara Menggunakan

1. **Jalankan bot:**
```bash
python bot.py
```

2. **Buka Telegram dan cari bot Anda**

3. **Kirim command `/start` untuk melihat instruksi**

4. **Kirim command `/login` untuk memulai proses login**

5. **Ikuti langkah-langkah:**
   - Kirim nomor telepon Anda (contoh: +628123456789)
   - Bot akan mengirimkan kode OTP ke Telegram Anda
   - Kirim kode OTP yang diterima
   - Jika akun Anda menggunakan 2FA, kirim password

6. **Setelah berhasil, session akan tersimpan di `sessions/users/[nomor_telepon].session`**

## Struktur Project

```
View-TL/
├── bot.py                 # Main bot file
├── telethon_client.py     # Handler untuk Telethon authentication
├── config.py              # Konfigurasi dan environment variables
├── requirements.txt       # Dependencies
├── .env                   # Environment variables (buat sendiri)
├── .env.example          # Contoh file .env
├── README.md             # Dokumentasi
└── sessions/
    └── users/            # Folder untuk menyimpan session (otomatis dibuat)
```

## Command Bot

- `/start` - Menampilkan pesan selamat datang dan instruksi
- `/login` - Memulai proses login
- `/cancel` - Membatalkan proses login yang sedang berlangsung

## Keamanan

⚠️ **PENTING:**
- Jangan share file `.env` atau session files dengan siapapun
- File session mengandung kredensial login Anda
- Tambahkan `sessions/` dan `.env` ke `.gitignore` jika menggunakan Git

## Troubleshooting

### Error: "BOT_TOKEN tidak ditemukan"
- Pastikan file `.env` sudah dibuat dan berisi `BOT_TOKEN`

### Error: "API_ID atau API_HASH tidak valid"
- Pastikan `API_ID` dan `API_HASH` sudah diisi dengan benar di file `.env`
- Pastikan `API_ID` adalah angka (tanpa tanda kutip)

### Error saat login
- Pastikan nomor telepon yang dikirim sudah benar
- Pastikan kode OTP masih valid (kode OTP biasanya berlaku 5 menit)
- Jika menggunakan 2FA, pastikan password yang dikirim benar

### Session tidak tersimpan
- Pastikan folder `sessions/users/` memiliki permission write
- Cek apakah ada error di console

## Lisensi

Project ini bebas digunakan untuk keperluan pribadi atau komersial.

## Kontribusi

Silakan buat issue atau pull request jika ada bug atau fitur yang ingin ditambahkan.

