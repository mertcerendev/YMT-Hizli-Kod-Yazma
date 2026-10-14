"""Tanıtım videosunu üretir: video.html'i kare kare çizdirip MP4'e çevirir.

Kullanım (bu klasörde):
  python uret.py                      -> logoyu-kodla-tanitim.mp4 (1080x1920; Instagram ve WhatsApp için)
  python uret.py --dortk              -> logoyu-kodla-tanitim-4k.mp4 (2160x3840 ana kopya; uzun sürer)
  python uret.py --olcek 1            -> hızlı taslak: 1080x1920, küçültme yok
  python uret.py --kare 0.5,3,12      -> yalnızca bu saniyelerin görüntüleri (kareler/ klasörüne)
  python uret.py --kart               -> kart.png'yi oyunun kendi kart çizimiyle yeniden üretir
                                         (sponsor ya da kart tasarımı değişince)
Çözünürlük: sayfa --olcek katı piksel yoğunluğunda (2 = 4K) çizilir. 1080p sürüm bu 4K görüntülerin
küçültülmesiyle elde edilir; doğrudan 1080p çizmekten daha keskin kenar ve yazı verir.
Hareket bulanıklığı: her kare, deklanşörün açık kaldığı süreye yayılmış birkaç ara görüntünün
ortalamasıdır (--ornek). Hızlı hareket eden öğeler bu yüzden kesik kesik değil, akıcı görünür.
Video, birkaç tarayıcının aynı anda çizdiği parçalar hâlinde üretilip birleştirilir (--is).
Ses: video bitince ses.py çağrılır; efekt seslerini ve müzik yatağını üretip dosyaya ekler.
Bellek: 4K ana kopyada her tarayıcı ve kodlayıcı çifti 3 GB kadar bellek ister; o yüzden 2 iş yeter.
Gerekenler: playwright (bilgisayardaki Edge'i kullanır), imageio-ffmpeg, numpy, Pillow.
"""
import argparse
import base64
import io
import pathlib
import subprocess
import sys

from playwright.sync_api import sync_playwright

KLASOR = pathlib.Path(__file__).resolve().parent
KARE_HIZI = 30
EN, BOY = 1080, 1920
DEKLANSOR = .5   # karenin ne kadarında deklanşör açık (0,5 = sinemadaki 180 derece)

# Videodaki kartın üstünde yazanlar (bkz. video.html, SON_SURE)
KART = """{ no: '00000', ad: 'Senin', soyad: 'Adın', altYazi: 'Senin bölümün',
  unvan: UNVANLAR[0].ad, renk: UNVANLAR[0].renk, rozet: 'Birinci', rozetTuru: 1,
  buyuk: '00:31.204', ek: '', wpm: 63, sira: '1.', hata: 0 }"""

secenek = argparse.ArgumentParser()
secenek.add_argument("--kare", help="virgülle ayrılmış saniyeler; verilirse video yerine bu kareler kaydedilir")
secenek.add_argument("--kart", action="store_true", help="kart.png'yi oyundan yeniden üret")
secenek.add_argument("--dortk", action="store_true", help="1080p yerine 2160x3840 ana kopyayı üret")
secenek.add_argument("--cikti", help="çıktı dosyası ya da (--kare ile) klasörü")
secenek.add_argument("--olcek", type=int, default=2, help="çizimin piksel yoğunluğu: 1 = 1080x1920, 2 = 2160x3840")
secenek.add_argument("--ornek", type=int, default=6, help="kare başına ara görüntü sayısı (1: bulanıklık yok)")
secenek.add_argument("--is", dest="is_sayisi", type=int, help="aynı anda çizen tarayıcı sayısı (varsayılan: 4, 4K'da 2)")
secenek.add_argument("--parca", help="(uret.py kendi çağırır) i/n: videonun n parçasından i.sini üret")
a = secenek.parse_args()


def ffmpeg_yolu():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def goruntu_al(sayfa, cdp, sn):
    """sn anını çizdirir ve ekran görüntüsünü PNG baytları olarak döndürür."""
    sayfa.evaluate("ciz(%f)" % sn)
    # Kırpma alanındaki 'scale' verilmezse görüntü, piksel yoğunluğu ne olursa olsun 1080x1920 çıkar
    kirpma = {"x": 0, "y": 0, "width": EN, "height": BOY, "scale": a.olcek}
    return base64.b64decode(cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True, "clip": kirpma})["data"])


def ara_anlar(kare_no):
    """Bir karenin deklanşör açıkken alınan ara görüntülerinin saniyeleri."""
    return [(kare_no + k / a.ornek * DEKLANSOR) / KARE_HIZI for k in range(a.ornek)]


def parcayi_uret(sayfa, cdp, bas, bit, hedef):
    """bas-bit arasındaki kareleri hedef dosyaya yazar. Ara görüntülerin ortalamasını ffmpeg alır;
    ortalama, ekranın gerçekte yaydığı ışık üzerinden alınsın diye gama düzeltmesi önce geri alınır."""
    n = a.ornek
    isiga = "lut=c0='pow(val/65535,2.2)*65535':c1='pow(val/65535,2.2)*65535':c2='pow(val/65535,2.2)*65535'"
    gamaya = "lut=c0='pow(val/65535,1/2.2)*65535':c1='pow(val/65535,1/2.2)*65535':c2='pow(val/65535,1/2.2)*65535'"
    # 1080p sürümde her ara görüntü önce küçültülür: 4K çizimin keskinliği kalır, bellek ve süre azalır
    kucult = [] if a.dortk or a.olcek == 1 else ["scale=%d:%d:flags=lanczos" % (EN, BOY)]
    ortalama = [] if n == 1 else [
        "format=gbrp16le", isiga, "tmix=frames=%d" % n,
        "select='eq(mod(n\\,%d)\\,%d)'" % (n, n - 1), "setpts=N/%d/TB" % KARE_HIZI, gamaya,
    ]
    kalite = (["-preset", "medium", "-crf", "15", "-threads", "6", "-x264-params", "rc-lookahead=12"] if a.dortk
              else ["-preset", "slow", "-crf", "16", "-level", "4.1"])
    # H.264, yuv420p, BT.709 etiketli: Instagram, WhatsApp ve telefonların kendi oynatıcısı sorunsuz açar
    ffmpeg = subprocess.Popen([
        ffmpeg_yolu(), "-y", "-loglevel", "error",
        "-f", "image2pipe", "-framerate", str(KARE_HIZI * n), "-c:v", "png", "-i", "-",
        "-vf", ",".join(kucult + ortalama + ["scale=in_range=pc:out_range=tv:out_color_matrix=bt709", "format=yuv420p"]),
        "-c:v", "libx264", "-profile:v", "high", *kalite,
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-r", str(KARE_HIZI), str(hedef),
    ], stdin=subprocess.PIPE)
    for i in range(bas, bit):
        for sn in ara_anlar(i):
            ffmpeg.stdin.write(goruntu_al(sayfa, cdp, sn))
    ffmpeg.stdin.close()
    if ffmpeg.wait():
        sys.exit("ffmpeg hata verdi")


def tarayici_ac(p):
    tarayici = p.chromium.launch(channel="msedge", headless=True, args=["--hide-scrollbars"])
    sayfa = tarayici.new_page(viewport={"width": EN, "height": BOY}, device_scale_factor=a.olcek)
    return tarayici, sayfa


def videoyu_ac(sayfa):
    sayfa.goto((KLASOR / "video.html").as_uri() + "?uretim")
    sayfa.wait_for_function("window.hazir === true")
    return sayfa.context.new_cdp_session(sayfa)


if a.kart:
    with sync_playwright() as p:
        tarayici, sayfa = tarayici_ac(p)
        # Kartın zeminindeki silik kod dokusu da videodaki programdan gelir: oyunun gerçek satırları videoda görünmesin
        videoyu_ac(sayfa)
        kodlar = sayfa.evaluate("PROGRAM.map((p) => p.kod)")
        sayfa.goto((KLASOR.parent / "index.html").as_uri())
        sayfa.wait_for_function("kartLogo.complete && sponsorLogo.complete")
        sayfa.evaluate("(kodlar) => kodlar.forEach((k, i) => { PROGRAM[i].kod = k; })", kodlar)
        # Kart iki kat çözünürlükte çizilir (video 4K çizilirken de keskin kalsın). Oyun tuvali 1080x1920
        # açar; araya giren sahte tuval gerçek tuvali iki katı açıp çizimi iki kat büyütür. Oyundaki küçük
        # gömülü logo yerine de logo.png'nin kendisi verilir.
        logo = base64.b64encode((KLASOR.parent / "logo.png").read_bytes()).decode()
        veri = sayfa.evaluate("""async (logo) => {
          kartLogo.src = 'data:image/png;base64,' + logo;
          await kartLogo.decode();
          const gercek = document.createElement('canvas'), KAT = 2;
          const sahte = {
            set width(v) { gercek.width = v * KAT; }, get width() { return gercek.width / KAT; },
            set height(v) { gercek.height = v * KAT; }, get height() { return gercek.height / KAT; },
            getContext(tur) { const c = gercek.getContext(tur); c.setTransform(KAT, 0, 0, KAT, 0, 0); return c; },
          };
          kartiCiz(%s, sahte);
          return gercek.toDataURL('image/png');
        }""" % KART, logo)
        (KLASOR / "kart.png").write_bytes(base64.b64decode(veri.split(",", 1)[1]))
        tarayici.close()
    print("tamam: kart.png")

elif a.kare:
    import numpy as np
    from PIL import Image

    isiga = (np.arange(256) / 255.0) ** 2.2
    hedef = pathlib.Path(a.cikti) if a.cikti else KLASOR / "kareler"
    hedef.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        tarayici, sayfa = tarayici_ac(p)
        cdp = videoyu_ac(sayfa)
        for sn in (float(x) for x in a.kare.split(",")):
            toplam = sum(isiga[np.asarray(Image.open(io.BytesIO(goruntu_al(sayfa, cdp, t))).convert("RGB"))]
                         for t in ara_anlar(sn * KARE_HIZI))
            Image.fromarray(np.rint((toplam / a.ornek) ** (1 / 2.2) * 255).astype(np.uint8)).save(hedef / ("%06.2f.png" % sn))
        tarayici.close()
    print("tamam:", hedef)

elif a.parca:
    i, n = (int(x) for x in a.parca.split("/"))
    with sync_playwright() as p:
        tarayici, sayfa = tarayici_ac(p)
        cdp = videoyu_ac(sayfa)
        adet = round(sayfa.evaluate("SURE") * KARE_HIZI)
        parcayi_uret(sayfa, cdp, adet * i // n, adet * (i + 1) // n, a.cikti)
        tarayici.close()

else:
    cikti = pathlib.Path(a.cikti) if a.cikti else KLASOR / ("logoyu-kodla-tanitim-4k.mp4" if a.dortk else "logoyu-kodla-tanitim.mp4")
    is_sayisi = a.is_sayisi or (2 if a.dortk else 4)
    on_ek = "parca4k" if a.dortk else "parca"   # iki sürüm aynı anda üretilebilsin diye parça adları ayrı
    parcalar = [KLASOR / ("%s-%d.mp4" % (on_ek, i)) for i in range(is_sayisi)]
    isler = [subprocess.Popen([sys.executable, __file__, "--parca", "%d/%d" % (i, is_sayisi), "--cikti", str(yol),
                               "--ornek", str(a.ornek), "--olcek", str(a.olcek)] + (["--dortk"] if a.dortk else []))
             for i, yol in enumerate(parcalar)]
    if any([i.wait() for i in isler]):
        sys.exit("parçalardan biri üretilemedi")
    liste = KLASOR / (on_ek + ".txt")
    liste.write_text("".join("file '%s'\n" % yol.name for yol in parcalar), encoding="utf-8")
    birlestir = subprocess.run([ffmpeg_yolu(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(liste),
                                "-c", "copy", "-movflags", "+faststart", str(cikti)])
    for yol in parcalar + [liste]:
        yol.unlink()
    if birlestir.returncode:
        sys.exit("parçalar birleştirilemedi")
    print("tamam: %s (%.1f MB)" % (cikti, cikti.stat().st_size / 1e6))
    # Ses: ses.py olayların saniyelerini video.html'den okuyup sesi üretir ve dosyaya ekler (görüntü yeniden kodlanmaz)
    subprocess.run([sys.executable, str(KLASOR / "ses.py"), "--dosya", str(cikti)], check=True)
