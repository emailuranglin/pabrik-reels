"""
PABRIK REELS - kantor virtual AI gratis untuk Page Facebook (niche drama keluarga)

Karyawan AI:
  1. Perencana  (Gemini)          -> memilih ide kisah baru
  2. Penulis    (Gemini)          -> menulis naskah, adegan, caption
  3. Pemeriksa  (Gemini)          -> QC naskah, minta revisi kalau perlu
  4. Ilustrator (Cloudflare FLUX) -> membuat gambar siluet tiap adegan
  5. Narator    (edge-tts)        -> membuat suara narasi
  6. Editor     (ffmpeg)          -> merakit video vertikal + subtitle
  7. Pengunggah (Facebook API)    -> memposting Reels ke Page

Mode (diatur dari GitHub Actions):
  MODE=uji      -> hanya membuat video, TIDAK diposting (untuk dicek dulu)
  MODE=posting  -> membuat video lalu langsung memposting ke Page
"""

import asyncio
import base64
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
SUARA = os.environ.get("SUARA", "id-ID-GadisNeural")  # atau id-ID-ArdiNeural (pria)

# Model Gemini dicoba berurutan; yang pertama berhasil dipakai.
MODEL_GEMINI = [
    m.strip()
    for m in os.environ.get(
        "MODEL_GEMINI",
        "gemini-flash-latest,gemini-flash-lite-latest,gemini-2.5-flash,gemini-2.0-flash",
    ).split(",")
    if m.strip()
]
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
async def _tts(teks, path):
    import edge_tts

    await edge_tts.Communicate(teks, SUARA, rate="+4%").save(path)


def karyawan_narator(teks, path):
    for percobaan in range(3):
        try:
            asyncio.run(_tts(teks, path))
            if os.path.getsize(path) > 1000:
                return
        except Exception as e:  # noqa: BLE001
            log(f"  TTS gagal ({e}), coba lagi...")
            time.sleep(4)
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


def buat_latar(judul, kalimat, warna, path):
    """Latar 1080x1920: judul di atas, kotak kosong untuk gambar, subtitle di bawah."""
    img = Image.new("RGB", (W, H), warna)
    d = ImageDraw.Draw(img)
    # judul
    tulis_tengah(d, judul.upper(), 120, font(54), 28, (255, 230, 150))
    # bingkai gambar
    x0 = (W - UKURAN_GAMBAR) // 2
    y0 = 380
    d.rounded_rectangle(
        [x0 - 8, y0 - 8, x0 + UKURAN_GAMBAR + 8, y0 + UKURAN_GAMBAR + 8],
        radius=24, fill=(255, 255, 255),
    )
    # subtitle
    fnt = font(58)
    baris = textwrap.wrap(kalimat, width=26)
    tinggi_teks = len(baris) * int(fnt.size * 1.25)
    y_sub = 1440
    pad = 28
    d.rounded_rectangle(
        [50, y_sub - pad, W - 50, y_sub + tinggi_teks + pad - 10],
        radius=28, fill=(0, 0, 0),
    )
    tulis_tengah(d, kalimat, y_sub, fnt, 26, (255, 255, 255), bayangan=False)
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


def rakit_klip(latar, gambar, suara, pos, out):
    """Satu klip: latar + gambar yang pelan-pelan zoom + suara."""
    dur = durasi(suara) + 0.35
    frame = math.ceil(dur * FPS)
    x0, y0 = pos
    s = UKURAN_GAMBAR
    zoom = (
        f"[1:v]scale={s * 2}:{s * 2},"
        f"zoompan=z='min(zoom+0.0007,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frame}:s={s}x{s}:fps={FPS}[z];"
        f"[0:v][z]overlay={x0}:{y0}:shortest=1,format=yuv420p[v];"
        f"[2:a]apad=pad_dur=0.35[a]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-loop", "1", "-framerate", str(FPS), "-i", latar,
         "-i", gambar,
         "-i", suara,
         "-filter_complex", zoom,
         "-map", "[v]", "-map", "[a]",
         "-t", f"{dur:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS),
         "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
         out],
        check=True,
    )


def karyawan_editor(naskah):
    log("\n[4-6] ILUSTRATOR + NARATOR + EDITOR: membuat video...")
    os.makedirs(FOLDER, exist_ok=True)
    klip = []
    nomor = 0
    for i, adegan in enumerate(naskah["adegan"], start=1):
        log(f"  Adegan {i}/{len(naskah['adegan'])}: membuat gambar...")
        gambar = f"{FOLDER}/gambar_{i}.jpg"
        karyawan_ilustrator(adegan["prompt_gambar"], gambar)
        warna = WARNA_LATAR[(i - 1) % len(WARNA_LATAR)]
        for kalimat in pecah_kalimat(adegan["narasi"]):
            nomor += 1
            suara = f"{FOLDER}/suara_{nomor}.mp3"
            karyawan_narator(kalimat, suara)
            latar = f"{FOLDER}/latar_{nomor}.png"
            pos = buat_latar(naskah["judul"], kalimat, warna, latar)
            out = f"{FOLDER}/klip_{nomor:03d}.mp4"
            rakit_klip(latar, gambar, suara, pos, out)
            klip.append(out)
    daftar = f"{FOLDER}/daftar.txt"
    with open(daftar, "w") as f:
        for k in klip:
            f.write(f"file '{os.path.basename(k)}'\n")
    hasil = f"{FOLDER}/video_final.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", daftar, "-c", "copy", "-movflags", "+faststart", hasil],
        check=True,
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
