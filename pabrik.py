"""
PABRIK REELS - kantor virtual AI gratis untuk Page Facebook (niche drama keluarga)

Karyawan AI:
  1. Perencana  (Gemini)          -> memilih ide kisah baru
  2. Penulis    (Gemini)          -> menulis naskah, adegan, caption
  3. Pemeriksa  (Gemini)          -> QC naskah, minta revisi kalau perlu
  4. Ilustrator (Cloudflare FLUX) -> membuat gambar siluet tiap adegan
  5. Narator    (Gemini TTS, cadangan edge-tts) -> membacakan kisah
  6. Editor     (ffmpeg)          -> merakit video vertikal + subtitle
  7. Pengunggah (Facebook API)    -> memposting Reels ke Page

Mode (diatur dari GitHub Actions):
  MODE=uji      -> hanya membuat video, TIDAK diposting (untuk dicek dulu)
  MODE=posting  -> membuat video lalu langsung memposting ke Page
"""

import asyncio
import base64
import difflib
import glob
import json
import math
import os
import random
import re
import subprocess
import sys
import textwrap
import time

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- pengaturan
MODE = os.environ.get("MODE", "uji").strip().lower()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
FB_TOKEN = os.environ.get("FB_PAGE_TOKEN", "").strip()

NAMA_PAGE = os.environ.get("NAMA_PAGE", "").strip()
# Mesin suara: auto = coba Gemini TTS (lebih ekspresif), kalau gagal pakai edge-tts
MESIN_SUARA = os.environ.get("MESIN_SUARA", "auto").strip().lower() or "auto"
SUARA_GEMINI = os.environ.get("SUARA_GEMINI", "").strip() or "Sulafat"  # suara hangat
MODEL_SUARA = [
    m.strip()
    for m in (os.environ.get("MODEL_SUARA", "").strip() or
              "gemini-3.8-flash-tts,gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts"
              ).split(",")
    if m.strip()
]
SUARA = os.environ.get("SUARA", "").strip() or "id-ID-GadisNeural"  # cadangan edge-tts

# Model Gemini dicoba berurutan; yang pertama berhasil dipakai.
MODEL_GEMINI = [
    m.strip()
    for m in os.environ.get(
        "MODEL_GEMINI",
        "gemini-flash-latest,gemini-flash-lite-latest,gemini-2.5-flash,gemini-2.0-flash",
    ).split(",")
    if m.strip()
]
MODEL_WHISPER = os.environ.get("MODEL_WHISPER", "").strip() or "base"
MODEL_GAMBAR = os.environ.get("MODEL_GAMBAR", "@cf/black-forest-labs/flux-1-schnell")

FOLDER = "hasil"
RIWAYAT = "riwayat.json"
W, H = 1080, 1920          # ukuran video vertikal Reels
FPS = 30
UKURAN_GAMBAR = 1000        # gambar ilustrasi persegi di tengah layar

GAYA_GAMBAR = (
    "minimalist flat 2D illustration, simple black silhouette characters without faces, "
    "clean simple shapes, soft plain pastel background, warm emotional lighting, "
    "Indonesian family home setting, no text, no letters, no watermark"
)

WARNA_LATAR = [
    (44, 62, 80), (94, 64, 52), (52, 73, 94), (74, 56, 92),
    (58, 84, 70), (110, 70, 60), (60, 60, 75),
]


def log(pesan):
    print(pesan, flush=True)


def wajib(nama, nilai):
    if not nilai:
        sys.exit(f"[GAGAL] Secret {nama} belum diisi di GitHub (Settings > Secrets > Actions).")


# ---------------------------------------------------------------- Gemini
_model_aktif = None


def tanya_gemini(perintah, json_mode=True, suhu=0.9):
    """Kirim perintah ke Gemini, kembalikan teks (atau dict jika json_mode)."""
    global _model_aktif
    daftar = [_model_aktif] if _model_aktif else MODEL_GEMINI
    error_terakhir = None
    for model in daftar:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_KEY}"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": perintah}]}],
            "generationConfig": {"temperature": suhu},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        for percobaan in range(4):
            try:
                r = requests.post(url, json=body, timeout=120)
            except requests.RequestException as e:
                error_terakhir = str(e)
                time.sleep(5)
                continue
            if r.status_code == 200:
                data = r.json()
                try:
                    teks = data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError):
                    error_terakhir = f"jawaban kosong: {json.dumps(data)[:300]}"
                    time.sleep(3)
                    continue
                _model_aktif = model
                if not json_mode:
                    return teks.strip()
                try:
                    return json.loads(teks)
                except json.JSONDecodeError:
                    cocok = re.search(r"\{.*\}", teks, re.S)
                    if cocok:
                        return json.loads(cocok.group(0))
                    error_terakhir = "jawaban bukan JSON"
                    continue
            if r.status_code in (429, 500, 503):
                tunggu = 20 * (percobaan + 1)
                log(f"  Gemini sibuk/limit ({r.status_code}), tunggu {tunggu} detik...")
                time.sleep(tunggu)
                error_terakhir = r.text[:300]
                continue
            # 404 / 400 model tidak ada -> coba model berikutnya
            error_terakhir = f"{model}: {r.status_code} {r.text[:300]}"
            break
        log(f"  Model {model} tidak bisa dipakai, coba model lain...")
    sys.exit(f"[GAGAL] Gemini tidak menjawab. Detail: {error_terakhir}")


# ---------------------------------------------------------------- Karyawan 1-3
def baca_riwayat():
    if os.path.exists(RIWAYAT):
        with open(RIWAYAT, encoding="utf-8") as f:
            return json.load(f)
    return []


TEMA = [
    "pengorbanan ibu yang baru disadari anaknya setelah dewasa",
    "ayah pendiam yang diam-diam berjuang untuk keluarga",
    "anak yang merantau dan lupa pulang",
    "menantu dan mertua yang awalnya tidak akur lalu saling memahami",
    "kakak yang mengalah demi pendidikan adiknya",
    "suami istri yang diuji kesulitan ekonomi",
    "nenek atau kakek yang kesepian di masa tua",
    "anak yang malu dengan pekerjaan orang tuanya",
    "saudara kandung yang bertengkar karena warisan lalu berdamai",
    "orang tua yang membandingkan anak-anaknya",
    "kebaikan kecil tetangga yang mengubah sebuah keluarga",
    "anak yang durhaka lalu menyesal",
]


def karyawan_perencana(riwayat):
    log("\n[1] PERENCANA: mencari ide kisah...")
    judul_lama = [r.get("judul", "") for r in riwayat[-40:]]
    tema = random.choice(TEMA)
    perintah = f"""Kamu perencana konten Page Facebook Indonesia bertema DRAMA KELUARGA penuh hikmah.
Buat SATU ide kisah fiksi baru untuk video Reels 60 detik.
Arah tema hari ini: {tema}.

Syarat:
- Kisah fiksi sehari-hari keluarga Indonesia, mengharukan, ada kejutan/plot twist di akhir, dan ada pelajaran hidup.
- Tidak boleh mirip judul-judul yang sudah pernah dibuat ini: {json.dumps(judul_lama, ensure_ascii=False)}
- Tidak ada kekerasan sadis, tidak ada konten dewasa, tidak menyinggung SARA.
- Jangan mengutip ayat/hadis/tokoh nyata.

Jawab JSON: {{"judul": "...", "ringkasan": "3-4 kalimat alur kisah dari awal sampai twist", "pelajaran": "1 kalimat"}}"""
    ide = tanya_gemini(perintah)
    log(f"  Ide: {ide.get('judul')}")
    return ide


def karyawan_penulis(ide, catatan_revisi=None):
    log("\n[2] PENULIS: menulis naskah..." + (" (revisi)" if catatan_revisi else ""))
    revisi = ""
    if catatan_revisi:
        revisi = f"\nIni revisi. Perbaiki sesuai catatan pemeriksa: {catatan_revisi}\n"
    perintah = f"""Kamu penulis naskah video Reels Facebook berbahasa Indonesia yang hangat dan menyentuh.
Tulis naskah berdasarkan ide ini:
{json.dumps(ide, ensure_ascii=False)}
{revisi}
Aturan naskah:
- Total narasi 120-150 kata (sekitar 55-65 detik dibacakan).
- Kalimat pembuka (hook) harus langsung bikin penasaran, maksimal 12 kata.
- Bagi menjadi 5 atau 6 adegan. Tiap adegan 1-3 kalimat pendek. Bahasa lisan sehari-hari, mudah dibacakan.
- Adegan terakhir berisi twist + pelajaran, lalu ajakan ringan berkomentar (contoh: "Kalau kamu jadi dia, apa yang akan kamu lakukan?").
- Untuk tiap adegan, tulis prompt_gambar dalam BAHASA INGGRIS: deskripsi visual singkat (siapa, sedang apa, di mana),
  tokoh berupa siluet hitam tanpa wajah. Jangan menyebut teks/tulisan di gambar.
- Caption Facebook: 2-3 kalimat yang memancing rasa penasaran + pertanyaan untuk komentar. Jangan clickbait palsu.
- 4-6 hashtag bahasa Indonesia yang relevan.

Jawab JSON:
{{"judul": "...", "adegan": [{{"narasi": "...", "prompt_gambar": "..."}}],
  "caption": "...", "hashtag": ["#...", "#..."]}}"""
    return tanya_gemini(perintah)


def karyawan_pemeriksa(naskah):
    log("\n[3] PEMERIKSA: mengecek kualitas naskah...")
    perintah = f"""Kamu editor quality control untuk Page Facebook drama keluarga. Periksa naskah ini dengan teliti:
{json.dumps(naskah, ensure_ascii=False)}

Cek:
1. Bahasa Indonesia benar, alami, enak dibacakan, tidak ada kalimat aneh.
2. Alur masuk akal, hook kuat, twist dan pelajaran jelas.
3. Panjang total narasi 110-160 kata, 5-6 adegan.
4. Aman monetisasi: tanpa kekerasan sadis, konten dewasa, SARA, ujaran kebencian, informasi medis/agama yang keliru.
5. Tidak mengaku kisah nyata, tidak memakai nama tokoh/merek nyata.
6. Caption tidak clickbait yang menipu.
7. Semua prompt_gambar berbahasa Inggris dan tidak meminta teks di dalam gambar.

Jawab JSON: {{"lolos": true/false, "skor": 1-10, "catatan": "apa yang harus diperbaiki (kosongkan jika lolos)"}}
Beri lolos=true hanya jika skor >= 7 dan tidak ada masalah keamanan."""
    hasil = tanya_gemini(perintah, suhu=0.2)
    log(f"  Skor: {hasil.get('skor')} | Lolos: {hasil.get('lolos')} | {hasil.get('catatan', '')}")
    return hasil


# ---------------------------------------------------------------- Karyawan 4
def karyawan_ilustrator(prompt, path):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{MODEL_GAMBAR}"
    body = {"prompt": f"{prompt}. Style: {GAYA_GAMBAR}", "steps": 4}
    for percobaan in range(3):
        try:
            r = requests.post(
                url, headers={"Authorization": f"Bearer {CF_TOKEN}"}, json=body, timeout=120
            )
        except requests.RequestException as e:
            log(f"  Koneksi Cloudflare gagal: {e}")
            time.sleep(5)
            continue
        if r.status_code == 200:
            ctype = r.headers.get("content-type", "")
            if ctype.startswith("image/"):
                data = r.content
            else:
                hasil = r.json().get("result", {})
                data = base64.b64decode(hasil["image"])
            with open(path, "wb") as f:
                f.write(data)
            return
        log(f"  Cloudflare error {r.status_code}: {r.text[:300]}")
        if r.status_code in (401, 403):
            sys.exit("[GAGAL] Token/Account ID Cloudflare salah atau kuota gratis habis hari ini.")
        time.sleep(8)
    sys.exit("[GAGAL] Gambar tidak berhasil dibuat.")


# ---------------------------------------------------------------- Karyawan 5
# Arahan gaya dibuat SINGKAT dengan pola resmi "Say ...:" supaya tidak ikut dibacakan.
GAYA_BACA = (
    os.environ.get("GAYA_SUARA", "").strip()
    or "Say in a warm, gentle, emotional storyteller voice, slightly slow, in Indonesian:"
)
if not GAYA_BACA.endswith(":"):
    GAYA_BACA += ":"


def perkiraan_detik(teks):
    """Perkiraan lama narasi dibaca pelan: sekitar 2,2 kata per detik."""
    return len(teks.split()) / 2.2


def tts_gemini(teks, path_wav):
    """Suara ekspresif dari Gemini TTS (gratis, kuota harian terbatas). True jika berhasil."""
    # percobaan 1: dengan arahan gaya; percobaan 2: teks polos (kalau arahan ikut terbaca)
    for pakai_gaya in (True, False):
        hasil = _tts_gemini_sekali(teks, path_wav, pakai_gaya)
        if hasil == "ok":
            return True
        if hasil != "kepanjangan":
            return False
    return False


def _tts_gemini_sekali(teks, path_wav, pakai_gaya):
    isi = f"{GAYA_BACA}\n{teks}" if pakai_gaya else teks
    for model in MODEL_SUARA:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_KEY}"
        )
        body = {
            "contents": [{"parts": [{"text": isi}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": SUARA_GEMINI}}
                },
            },
        }
        for percobaan in range(2):
            try:
                r = requests.post(url, json=body, timeout=300)
            except requests.RequestException as e:
                log(f"  Gemini TTS koneksi gagal: {e}")
                time.sleep(5)
                continue
            if r.status_code == 200:
                try:
                    bagian = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                except (KeyError, IndexError):
                    log(f"  Gemini TTS ({model}) tidak mengirim audio.")
                    break
                pcm = base64.b64decode(bagian["data"])
                rate = 24000
                cocok = re.search(r"rate=(\d+)", bagian.get("mimeType", ""))
                if cocok:
                    rate = int(cocok.group(1))
                mentah = path_wav + ".pcm"
                with open(mentah, "wb") as f:
                    f.write(pcm)
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(rate),
                     "-ac", "1", "-i", mentah, path_wav],
                    check=True,
                )
                os.remove(mentah)
                lama = durasi(path_wav)
                batas_atas = perkiraan_detik(teks) * 1.25 + 3
                if lama > batas_atas:
                    log(f"  Audio {lama:.0f} dtk, terlalu panjang (maks {batas_atas:.0f}): "
                        "kemungkinan arahan ikut terbaca. Ulangi tanpa arahan gaya.")
                    return "kepanjangan"
                if lama > 10:
                    log(f"  Suara dari Gemini TTS ({model}, suara {SUARA_GEMINI}).")
                    return "ok"
                log("  Audio Gemini terlalu pendek, coba lagi...")
                continue
            if r.status_code == 429:
                log(f"  Kuota Gemini TTS ({model}) habis/sibuk.")
                break
            if r.status_code in (500, 503):
                time.sleep(15)
                continue
            log(f"  Gemini TTS ({model}) tidak tersedia: {r.status_code}")
            break
    return "gagal"


async def _tts_edge(teks, path):
    import edge_tts

    await edge_tts.Communicate(teks, SUARA, rate="-6%", pitch="-2Hz").save(path)


def tts_edge(teks, path_wav):
    mp3 = path_wav.replace(".wav", "_edge.mp3")
    for percobaan in range(3):
        try:
            asyncio.run(_tts_edge(teks, mp3))
            if os.path.getsize(mp3) > 1000:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-ar", "24000",
                     "-ac", "1", path_wav],
                    check=True,
                )
                log(f"  Suara dari edge-tts ({SUARA}).")
                return True
        except Exception as e:  # noqa: BLE001
            log(f"  edge-tts gagal ({e}), coba lagi...")
            time.sleep(4)
    return False


def karyawan_narator(teks, path_wav):
    """Membacakan SELURUH kisah sekaligus supaya intonasinya mengalir alami."""
    log("  Narator: membacakan kisah...")
    if MESIN_SUARA in ("auto", "gemini") and tts_gemini(teks, path_wav):
        return
    if MESIN_SUARA == "gemini":
        log("  Gemini TTS gagal, pakai cadangan edge-tts.")
    if tts_edge(teks, path_wav):
        return
    sys.exit("[GAGAL] Suara narasi tidak berhasil dibuat.")


# ---------------------------------------------------------------- Karyawan 6
def cari_font(tebal=True):
    pola = ["*Bold.ttf", "*-Bold.ttf"] if tebal else ["*Regular.ttf", "DejaVuSans.ttf"]
    for p in pola:
        for folder in ("/usr/share/fonts", "/usr/local/share/fonts"):
            hasil = sorted(glob.glob(f"{folder}/**/{p}", recursive=True))
            dejavu = [h for h in hasil if "DejaVuSans" in h or "Liberation" in h]
            if dejavu:
                return dejavu[0]
            if hasil:
                return hasil[0]
    return None


FONT_TEBAL = cari_font(True)


def font(ukuran):
    if FONT_TEBAL:
        return ImageFont.truetype(FONT_TEBAL, ukuran)
    return ImageFont.load_default(size=ukuran)


def durasi(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def tulis_tengah(draw, teks, y, fnt, lebar_maks_karakter, warna, bayangan=True, jarak=1.25):
    baris = textwrap.wrap(teks, width=lebar_maks_karakter)
    tinggi = int(fnt.size * jarak)
    for b in baris:
        lebar = draw.textlength(b, font=fnt)
        x = (W - lebar) / 2
        if bayangan:
            draw.text((x + 3, y + 3), b, font=fnt, fill=(0, 0, 0))
        draw.text((x, y), b, font=fnt, fill=warna)
        y += tinggi
    return y


def buat_latar(judul, warna, path):
    """Latar 1080x1920: judul di atas, bingkai gambar di tengah. Subtitle ditempel belakangan."""
    img = Image.new("RGB", (W, H), warna)
    d = ImageDraw.Draw(img)
    tulis_tengah(d, judul.upper(), 120, font(54), 28, (255, 230, 150))
    x0 = (W - UKURAN_GAMBAR) // 2
    y0 = 330
    d.rounded_rectangle(
        [x0 - 8, y0 - 8, x0 + UKURAN_GAMBAR + 8, y0 + UKURAN_GAMBAR + 8],
        radius=24, fill=(255, 255, 255),
    )
    if NAMA_PAGE:
        tulis_tengah(d, NAMA_PAGE, H - 110, font(34), 40, (220, 220, 220), bayangan=False)
    img.save(path)
    return x0, y0


def pecah_kalimat(teks):
    bagian = re.split(r"(?<=[.!?])\s+", teks.strip())
    hasil = []
    for b in bagian:
        b = b.strip()
        if not b:
            continue
        # kalimat sangat panjang dipecah lagi di koma
        if len(b) > 110 and "," in b:
            potong = [p.strip() for p in b.split(",") if p.strip()]
            gabung = ""
            for p in potong:
                if len(gabung) + len(p) < 90:
                    gabung = f"{gabung}, {p}" if gabung else p
                else:
                    hasil.append(gabung + ",")
                    gabung = p
            if gabung:
                hasil.append(gabung)
        else:
            hasil.append(b)
    return hasil


def rakit_klip(latar, gambar, frame, pos, out):
    """Satu klip tanpa suara: latar + gambar yang pelan-pelan zoom, panjang = frame."""
    x0, y0 = pos
    s = UKURAN_GAMBAR
    filt = (
        f"[1:v]scale={s * 2}:{s * 2},"
        f"zoompan=z='min(zoom+0.0007,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frame}:s={s}x{s}:fps={FPS}[z];"
        f"[0:v][z]overlay={x0}:{y0},format=yuv420p[v]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-loop", "1", "-framerate", str(FPS), "-i", latar,
         "-i", gambar,
         "-filter_complex", filt, "-map", "[v]",
         "-frames:v", str(frame),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS),
         out],
        check=True,
    )


def bobot(kata):
    """Perkiraan lama sebuah kata dibacakan: jumlah huruf + jeda tanda baca."""
    jeda = 0
    if kata.endswith(","):
        jeda = 5
    elif kata[-1:] in ".!?":
        jeda = 10
    return len(kata) + 3 + jeda


def normal(kata):
    return re.sub(r"[^a-z0-9]", "", kata.lower())


def waktu_kata_whisper(kata_naskah, suara):
    """Cari waktu mulai tiap kata dengan mendengarkan ulang narasinya (faster-whisper).
    Mengembalikan list waktu mulai (detik) sepanjang kata_naskah, atau None jika gagal."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("  faster-whisper tidak terpasang, waktu subtitle pakai perkiraan.")
        return None
    try:
        log(f"  Menyelaraskan subtitle dengan suara (whisper {MODEL_WHISPER})...")
        model = WhisperModel(MODEL_WHISPER, device="cpu", compute_type="int8")
        segmen, _ = model.transcribe(suara, language="id", word_timestamps=True, beam_size=1)
        dengar = [(normal(w.word), w.start) for s in segmen for w in (s.words or [])]
    except Exception as e:  # noqa: BLE001
        log(f"  Whisper gagal ({e}), waktu subtitle pakai perkiraan.")
        return None
    dengar = [(k, t) for k, t in dengar if k]
    if len(dengar) < len(kata_naskah) * 0.5:
        log("  Whisper hanya menangkap sedikit kata, waktu subtitle pakai perkiraan.")
        return None
    a = [normal(k) for k in kata_naskah]
    b = [k for k, _ in dengar]
    waktu = [None] * len(a)
    cocok = 0
    for blok in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for n in range(blok.size):
            waktu[blok.a + n] = dengar[blok.b + n][1]
            cocok += 1
    log(f"  {cocok}/{len(a)} kata cocok dengan suara.")
    if cocok < len(a) * 0.4:
        return None
    return waktu


def isi_waktu_kosong(kata, waktu, total):
    """Kata yang tidak tertangkap whisper diberi waktu di antara tetangganya (sesuai panjang kata)."""
    n = len(kata)
    waktu = list(waktu)
    if waktu[0] is None:
        waktu[0] = 0.0
    i = 0
    while i < n:
        if waktu[i] is not None:
            i += 1
            continue
        mulai = i
        while i < n and waktu[i] is None:
            i += 1
        kiri = waktu[mulai - 1]
        kanan = waktu[i] if i < n else total - 0.5
        b = [bobot(k) for k in kata[mulai - 1:i]]
        jumlah = sum(b)
        t = kiri
        for j, idx in enumerate(range(mulai, i)):
            t += (kanan - kiri) * b[j] / jumlah
            waktu[idx] = t
    # pastikan selalu maju
    for i in range(1, n):
        if waktu[i] <= waktu[i - 1]:
            waktu[i] = waktu[i - 1] + 0.05
    return waktu


def waktu_kata_perkiraan(kata, total):
    b = [bobot(k) for k in kata]
    jumlah = sum(b)
    waktu, t = [], 0.0
    for x in b:
        waktu.append(t)
        t += total * x / jumlah
    return waktu


def ass_waktu(detik):
    detik = max(detik, 0)
    j = int(detik // 3600)
    m = int(detik % 3600 // 60)
    d = detik % 60
    return f"{j}:{m:02d}:{d:05.2f}"


def buat_subtitle_ass(kata, mulai, total, path):
    """Subtitle gaya 'per kata': satu kata besar muncul bergantian dengan efek membesar."""
    nama_font = "DejaVu Sans"
    if FONT_TEBAL and "Liberation" in FONT_TEBAL:
        nama_font = "Liberation Sans"
    kepala = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Kata,{nama_font},118,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,2,0,1,9,4,2,40,40,{H - 1530},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    baris = []
    for i, k in enumerate(kata):
        awal = mulai[i]
        akhir = mulai[i + 1] if i + 1 < len(kata) else min(awal + 1.2, total)
        akhir = min(akhir, awal + 1.5)
        teks = re.sub(r"[{}\\]", "", k).upper()
        bersih = normal(k)
        # kata panjang/penting diberi warna kuning, akhir kalimat seru/tanya merah muda
        if k[-1:] in "!?":
            warna = r"\c&H8080FF&"
        elif len(bersih) >= 6:
            warna = r"\c&H40D8FF&"
        else:
            warna = ""
        # kata yang terlalu panjang dikecilkan supaya tidak terpotong layar
        lebar = font(118).getlength(teks) + 2 * 9
        ukuran = 118 if lebar <= 900 else max(int(118 * 900 / lebar), 60)
        efek = (r"{\fs" + str(ukuran) +
                r"\fscx70\fscy70\t(0,90,\fscx108\fscy108)\t(90,160,\fscx100\fscy100)" + warna + "}")
        baris.append(f"Dialogue: 0,{ass_waktu(awal)},{ass_waktu(akhir)},Kata,,0,0,0,,{efek}{teks}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(kepala + "\n".join(baris) + "\n")


def karyawan_editor(naskah):
    log("\n[4-6] ILUSTRATOR + NARATOR + EDITOR: membuat video...")
    os.makedirs(FOLDER, exist_ok=True)

    # 1. susun daftar kata beserta nomor adegannya
    kata, adegan_kata = [], []
    for i, adegan in enumerate(naskah["adegan"], start=1):
        for k in adegan["narasi"].split():
            kata.append(k)
            adegan_kata.append(i)

    # 2. suara: seluruh kisah dibaca sekali jalan
    suara = f"{FOLDER}/narasi.wav"
    karyawan_narator(" ".join(kata), suara)
    lama_suara = durasi(suara)
    total = lama_suara + 0.8

    # 3. waktu tiap kata (didengarkan ulang, atau diperkirakan)
    waktu = waktu_kata_whisper(kata, suara)
    if waktu:
        mulai = isi_waktu_kosong(kata, waktu, lama_suara)
    else:
        mulai = waktu_kata_perkiraan(kata, lama_suara)

    # 4. satu klip per adegan, panjangnya mengikuti kapan adegan berikutnya mulai dibacakan
    jumlah_adegan = len(naskah["adegan"])
    awal_adegan = {}
    for idx, a in enumerate(adegan_kata):
        awal_adegan.setdefault(a, mulai[idx])
    awal_adegan[1] = 0.0
    batas = [awal_adegan.get(a, 0.0) for a in range(1, jumlah_adegan + 1)] + [total]
    frame_batas = [round(t * FPS) for t in batas]

    klip = []
    for a in range(1, jumlah_adegan + 1):
        log(f"  Adegan {a}/{jumlah_adegan}: membuat gambar...")
        gambar = f"{FOLDER}/gambar_{a}.jpg"
        karyawan_ilustrator(naskah["adegan"][a - 1]["prompt_gambar"], gambar)
        warna = WARNA_LATAR[(a - 1) % len(WARNA_LATAR)]
        latar = f"{FOLDER}/latar_{a}.png"
        pos = buat_latar(naskah["judul"], warna, latar)
        frame = max(frame_batas[a] - frame_batas[a - 1], 1)
        out = f"{FOLDER}/klip_{a:03d}.mp4"
        rakit_klip(latar, gambar, frame, pos, out)
        klip.append(out)

    # 5. sambung klip, tempel subtitle per kata + suara
    daftar = f"{FOLDER}/daftar.txt"
    with open(daftar, "w") as f:
        for k in klip:
            f.write(f"file '{os.path.basename(k)}'\n")
    gambar_saja = f"{FOLDER}/gambar_saja.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", daftar, "-c", "copy", gambar_saja],
        check=True,
    )
    sub = f"{FOLDER}/subtitle.ass"
    buat_subtitle_ass(kata, mulai, total, sub)
    hasil = f"{FOLDER}/video_final.mp4"
    filter_sub = "subtitles=subtitle.ass"
    if FONT_TEBAL:
        filter_sub += f":fontsdir={os.path.dirname(FONT_TEBAL)}"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", os.path.basename(gambar_saja), "-i", os.path.basename(suara),
         "-filter_complex", f"[0:v]{filter_sub}[v];[1:a]apad=pad_dur=1[a]",
         "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
         "-t", f"{durasi(gambar_saja):.2f}", "-movflags", "+faststart", os.path.basename(hasil)],
        check=True, cwd=FOLDER,
    )
    log(f"  Video jadi: {hasil} ({durasi(hasil):.1f} detik)")
    return hasil


# ---------------------------------------------------------------- Karyawan 7
def karyawan_pengunggah(video, caption):
    log("\n[7] PENGUNGGAH: memposting Reels ke Page Facebook...")
    base = f"https://graph.facebook.com/{FB_PAGE_ID}/video_reels"
    r = requests.post(base, data={"upload_phase": "start", "access_token": FB_TOKEN}, timeout=60)
    if r.status_code != 200:
        sys.exit(f"[GAGAL] Facebook menolak (start): {r.text[:500]}")
    mulai = r.json()
    video_id = mulai["video_id"]
    upload_url = mulai.get("upload_url") or f"https://rupload.facebook.com/video-upload/v21.0/{video_id}"
    ukuran = os.path.getsize(video)
    with open(video, "rb") as f:
        r = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {FB_TOKEN}",
                "offset": "0",
                "file_size": str(ukuran),
            },
            data=f,
            timeout=600,
        )
    if r.status_code != 200:
        sys.exit(f"[GAGAL] Upload video ke Facebook gagal: {r.text[:500]}")
    r = requests.post(
        base,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": FB_TOKEN,
        },
        timeout=60,
    )
    if r.status_code != 200 or not r.json().get("success", False):
        sys.exit(f"[GAGAL] Facebook menolak (finish): {r.text[:500]}")
    log(f"  Berhasil! ID video Reels: {video_id}")
    log("  Facebook masih memproses video beberapa menit sebelum muncul di Page.")
    return video_id


# ---------------------------------------------------------------- Bos
def main():
    log(f"=== PABRIK REELS mulai (mode: {MODE}) ===")
    wajib("GEMINI_API_KEY", GEMINI_KEY)
    wajib("CF_ACCOUNT_ID", CF_ACCOUNT)
    wajib("CF_API_TOKEN", CF_TOKEN)
    if MODE == "posting":
        wajib("FB_PAGE_ID", FB_PAGE_ID)
        wajib("FB_PAGE_TOKEN", FB_TOKEN)

    riwayat = baca_riwayat()
    ide = karyawan_perencana(riwayat)

    naskah, catatan = None, None
    for putaran in range(3):
        naskah = karyawan_penulis(ide, catatan)
        qc = karyawan_pemeriksa(naskah)
        if qc.get("lolos"):
            break
        catatan = qc.get("catatan") or "perbaiki kualitas secara umum"
    else:
        sys.exit("[BERHENTI] Naskah tidak lolos QC setelah 3 kali revisi. Tidak ada yang diposting.")

    naskah["adegan"] = naskah["adegan"][:7]
    video = karyawan_editor(naskah)

    hashtag = " ".join(naskah.get("hashtag", []))
    caption = (
        f"{naskah['caption']}\n\n"
        "📖 Cerita fiksi. Ilustrasi & suara dibuat dengan bantuan AI.\n\n"
        f"{hashtag} #kisahfiksi"
    )
    os.makedirs(FOLDER, exist_ok=True)
    with open(f"{FOLDER}/caption.txt", "w", encoding="utf-8") as f:
        f.write(caption)
    with open(f"{FOLDER}/naskah.json", "w", encoding="utf-8") as f:
        json.dump(naskah, f, ensure_ascii=False, indent=2)

    if MODE == "posting":
        video_id = karyawan_pengunggah(video, caption)
        riwayat.append({"judul": naskah["judul"], "video_id": video_id,
                        "waktu": time.strftime("%Y-%m-%d %H:%M")})
        with open(RIWAYAT, "w", encoding="utf-8") as f:
            json.dump(riwayat[-200:], f, ensure_ascii=False, indent=2)
    else:
        log("\nMODE UJI: video TIDAK diposting. Unduh hasilnya di bagian 'Artifacts'.")

    log("\n=== SELESAI ===")
    log(f"Judul  : {naskah['judul']}")
    log(f"Caption:\n{caption}")


if __name__ == "__main__":
    main()
