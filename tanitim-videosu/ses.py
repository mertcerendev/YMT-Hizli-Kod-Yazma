"""Tanıtım videosunun sesini üretir ve videoya ekler: görüntüdeki olaylarla eşzamanlı sesler ve
altlarında sakin bir müzik.

Kullanım (bu klasörde):
  python ses.py                 -> ses.wav üretir; logoyu-kodla-tanitim.mp4 ve -4k.mp4 dosyalarına ekler
  python ses.py --dosya x.mp4   -> yalnızca verilen videoya ekler
  python ses.py --muziksiz      -> notasız sürüm: yalnızca klavye, hışırtı ve vuruşlar (üstüne başka müzik eklenecekse)
  python ses.py --yalniz-ses    -> yalnızca ses.wav
  python ses.py --seslendirme seslendirme-elevenlabs-2.mp3 --dosya x.mp4   -> başka bir okumayla
Seslendirme: SESLENDIRMELER tablosundaki bir ses dosyasının cümleleri kesilip sahnelerine yerleştirilir ve
konuşma sırasında müzik kısılır. Hangi dosyanın kullanılacağı --seslendirme ile seçilir (varsayılan: SESLENDIRME;
"yok" yazılırsa video seslendirmesiz üretilir).
Müzik ve efektler kodla üretilir; hazır kayıt ya da başkasına ait müzik yoktur. Sesler elektronik "bip"ler
değil, gerçek çalgıların fizik modelleridir: mekanik klavye (tuşun ve kasanın tınlamaları), marimba
(tahta çubuğun titreşim kipleri), keçe çekiçli piyano (telin üst sesleri, çift tel), metal çubuk çan.
Eşzamanlılık: olayların saniyeleri (tuşlar, Enter'lar, sahne geçişleri) video.html'in kendisinden okunur;
sahnelerin içindeki küçük gecikmeler (ör. kartın ışıltısı) video.html'deki değerlerin aynısıdır.
Görüntü yeniden kodlanmaz (-c:v copy): ses eklemek görüntü kalitesini değiştirmez.
Gerekenler: numpy, playwright (bilgisayardaki Edge'i kullanır), imageio-ffmpeg.
"""
import argparse
import pathlib
import re
import subprocess
import sys
import wave

import numpy as np
from playwright.sync_api import sync_playwright

KLASOR = pathlib.Path(__file__).resolve().parent
WAV = KLASOR / "ses.wav"
SR = 48000             # örnekleme hızı (Hz)
HEDEF_LUFS = -15.0     # ses yüksekliği: sosyal medyanın hedeflediği düzeye yakın
TAVAN_DB = -1.5        # tepe sınırı (sıkıştırmada taşmasın diye pay bırakılır)
EN_COK_SINIRLAMA_DB = 4.0   # hedefe ulaşmak için tepelerin en fazla bu kadarı bastırılır; gerisi için ses kısık kalır
rng = np.random.default_rng(2026)   # sabit tohum: her çalıştırmada aynı ses

secenek = argparse.ArgumentParser()
secenek.add_argument("--dosya", nargs="*", help="sesin ekleneceği video(lar); verilmezse bu klasördeki iki sürüm")
secenek.add_argument("--muziksiz", action="store_true", help="notasız: yalnızca klavye, hışırtı ve vuruşlar")
secenek.add_argument("--yalniz-ses", action="store_true", help="videolara dokunma, yalnızca ses.wav üret")
secenek.add_argument("--seslendirme", help="kullanılacak seslendirme dosyası (SESLENDIRMELER'deki adlardan biri) ya da 'yok'")
a = secenek.parse_args()


# ================= YARDIMCILAR =================

def ornek(sure):
    return max(1, int(round(sure * SR)))


def zaman(sure):
    return np.arange(ornek(sure)) / SR


def db(x):
    return 10 ** (x / 20)


def faz(f):
    """Frekansı zamanla değişen bir sesin fazı (f: her örnek için Hz)."""
    return 2 * np.pi * np.cumsum(f) / SR


def nota(ad):
    """'A4' -> 440 Hz; diyez için 'F#4'."""
    yarim = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}[ad[0]] + ad.count("#")
    return 440 * 2 ** ((yarim + 12 * (int(ad[-1]) - 4)) / 12)


def notalar(adlar):
    return [nota(x) for x in adlar.split()]


def gecis(u):
    u = np.clip(u, 0, 1)
    return u * u * (3 - 2 * u)


def tepeye_gore(x):
    return x / np.abs(x).max()


def suz(x, alt=None, ust=None, egim=.6):
    """Frekans alanında süzgeç: alt ile ust (Hz) arasını geçirir; kenarları 'egim' oktav genişliğinde yumuşaktır."""
    n = len(x)
    lf = np.log2(np.maximum(np.fft.rfftfreq(n, 1 / SR), 1.0))
    maske = np.ones(len(lf))
    if alt:
        maske *= gecis((lf - np.log2(alt)) / egim + .5)
    if ust:
        maske *= gecis((np.log2(ust) - lf) / egim + .5)
    return np.fft.irfft((np.fft.rfft(x, axis=0).T * maske).T, n, axis=0)


def gurultu(sure, alt=None, ust=None):
    """Süzülmüş gürültü; ortalama gücü 1."""
    g = suz(rng.standard_normal(ornek(sure) + SR // 10), alt, ust)[: ornek(sure)]
    return g / np.sqrt(np.mean(g * g))


def kenar(x, bas=.003, son=.015):
    """Sesin başını ve sonunu yumuşatır: çıt sesi çıkmasın."""
    b, s = min(ornek(bas), len(x) // 2), min(ornek(son), len(x) // 2)
    zarf = np.ones(len(x))
    zarf[:b] = np.linspace(0, 1, b)
    zarf[-s:] = np.linspace(1, 0, s)
    return x * (zarf if x.ndim == 1 else zarf[:, None])


def seyrelt(anlar, aralik):
    """Birbirine 'aralik' saniyeden yakın olayları atar: ekrandaki yazı bir insanın yazabileceğinden hızlı aktığı
    için her harfe ses koymak takırtıya döner; kalan tuşlar yine ekrandaki bir harfle aynı anda duyulur."""
    secilen, son = [], -1.0
    for an in anlar:
        if an - son >= aralik:
            secilen.append(an)
            son = an
    return secilen


def evristir(x, iz):
    n = len(x) + len(iz) - 1
    uzunluk = 1 << (n - 1).bit_length()
    return np.fft.irfft(np.fft.rfft(x, uzunluk, axis=0) * np.fft.rfft(iz, uzunluk, axis=0), uzunluk, axis=0)[: len(x)]


def yanki_izi():
    """Yankının dürtü yanıtı: ılık, orta büyüklükte bir salon. Önce birkaç erken yansıma, ardından tizleri daha
    çabuk sönen yoğun bir kuyruk. Kanallar birbirinden bağımsız."""
    t = zaman(2.8)
    iz = np.zeros((len(t) + ornek(.015), 2))
    for kanal in range(2):
        kuyruk = gurultu(2.8, 180, 6500) * np.exp(-t / .22) * .8 + gurultu(2.8, 250, 1800) * np.exp(-t / .34)
        iz[ornek(.015):, kanal] = kuyruk * (1 - np.exp(-t / .012))       # kuyruk bir anda değil, dolarak başlar
        for k in range(6):                                                # erken yansımalar
            iz[ornek(.008 + .009 * k + .004 * rng.random()), kanal] += (1 if k % 2 else -1) * 30 * .75 ** k
    return iz / np.sqrt(np.sum(iz * iz, axis=0))


IZ = yanki_izi()


# ================= ÇALGILAR VE SESLER (tek kanal dönerler; "çift kanal" yazanlar hariç) =================

# Mekanik klavyenin tınlama kipleri: frekans (Hz), sönme süresi (sn), güç. Tok, "thock" denen türden bir kasa.
KLAVYE_KIPLERI = ((170, .030, .9), (410, .022, 1.0), (760, .014, .8), (1350, .009, .7), (2300, .006, .55), (3600, .004, .4), (5200, .003, .25))


def tik(kalin=1.0):
    """Tek bir tuş darbesi: 1,5 ms'lik bir çarpma, tuşun ve kasanın tınlamalarını uyarır. Kiplerin frekansı ve
    gücü her seferinde biraz değişir: iki tuş birbirinin aynısı çıkmaz. kalin < 1: daha büyük, tok tuş."""
    t = zaman(.16)
    govde = np.zeros(len(t))
    for f, tau, guc in KLAVYE_KIPLERI:
        f *= kalin * (1 + .14 * (rng.random() - .5))
        govde += guc * (.5 + rng.random()) * np.sin(2 * np.pi * f * t + 6.28 * rng.random()) * np.exp(-t * np.sqrt(kalin) / tau)
    darbe = rng.standard_normal(ornek(.0015)) * np.hanning(ornek(.0015))
    return kenar(tepeye_gore(np.convolve(govde, darbe)[: len(t)]), bas=.0003, son=.02)


def tus(kalin=1.0, derin=False):
    """Tuşa basış: iniş darbesi ve biraz sonra daha hafif kalkış tıkı. derin: Enter gibi büyük tuş;
    dengeleyici telinin takırtısı da duyulur ve tuş daha geç kalkar."""
    s = np.zeros(ornek(.34))

    def koy(parca, an, guc):
        i = int(an * SR)
        s[i:i + len(parca)] += parca[: len(s) - i] * guc

    koy(tik(kalin), 0, 1)
    if derin:
        koy(tik(kalin * 1.5), .013 + .004 * rng.random(), .35)
    koy(tik(kalin * 1.3), (.11 if derin else .065) + .03 * rng.random(), .3)
    return s


def marimba(f, guc=.7):
    """Marimba: tahta çubuğun üç titreşim kipi (1 : 3,93 : 9,6). Üst kipler çabuk söner, geriye yuvarlak temel
    ses kalır; başında yumuşak tokmağın tok darbesi vardır. guc: vuruşun sertliği (parlaklığı)."""
    tau = .42 * (330 / f) ** .6
    t = zaman(tau * 6)
    s = np.sin(2 * np.pi * f * t) * np.exp(-t / tau)
    for kat, pay, hiz, f0 in ((3.93, .38, .16, 1.0), (9.6, .12, .06, 2.0)):
        if f * kat < 14000:
            s += pay * guc * np.sin(2 * np.pi * f * kat * t + f0) * np.exp(-t / (tau * hiz))
    s *= np.minimum(t / .0015, 1)
    n = ornek(.03)
    s[:n] += gurultu(.03, 200, 1800) * np.exp(-t[:n] / .004) * .22 * guc
    return kenar(tepeye_gore(s))


def cubuk(f):
    """Metal çubuk çan (glockenspiel): serbest çubuğun kipleri 1 : 2,756 : 5,404 : 8,933; uzun, camsı bir tını."""
    tau = .9 * (1000 / f) ** .5
    t = zaman(tau * 5)
    s = np.zeros(len(t))
    for kat, pay, hiz in ((1, 1, 1), (2.756, .45, .55), (5.404, .2, .3), (8.933, .08, .18)):
        if f * kat < 16000:
            s += pay * np.sin(2 * np.pi * f * kat * t + 6.28 * rng.random()) * np.exp(-t / (tau * hiz))
    return kenar(tepeye_gore(s * np.minimum(t / .001, 1)), son=.05)


def piyano(f, guc=.6):
    """Keçe çekiçli piyano. Telin üst sesleri tam katlarda değil, biraz tizdedir (telin sertliği); her nota hafifçe
    farklı akortlu iki telden çıkar ve yavaşça dalgalanır; üst sesler çabuk, temel ses yavaş söner; başında
    çekicin tok sesi vardır. guc: tuşa basış sertliği (sert basınca ses parlaklaşır)."""
    yavas = 2.6 * (220 / f) ** .45
    t = zaman(min(4.0, yavas * 2.2))
    sertlik = .00035 * (f / 220) ** 1.4
    tel = 2 ** (.7 / 1200)   # iki tel arasında 1,4 sent
    s = np.zeros(len(t))
    for n in range(1, int(min(24, 7000 / f)) + 1):
        fn = n * f * np.sqrt(1 + sertlik * n * n)
        tau = yavas / (1 + .12 * (n - 1) ** 1.3)
        zarf = .75 * np.exp(-t / (tau * .28)) + .25 * np.exp(-t / tau)
        s += n ** -1.1 * np.exp(-fn / (2000 + 3200 * guc)) * zarf * (np.sin(2 * np.pi * fn * tel * t) + np.sin(2 * np.pi * fn / tel * t + .4 * n)) / 2
    s *= 1 - np.exp(-t / .005)
    s += gurultu(len(t) / SR, None, 350)[: len(t)] * np.exp(-t / .03) * .06 * np.abs(s).max()
    return kenar(tepeye_gore(s), son=.3)


def vurus():
    """Vuruş: sahne açılışlarındaki derin darbe. Alçalan bir alt bas, tok bir gövde ve kısa bir çarpma."""
    t = zaman(1.8)
    w = faz(38 + 55 * np.exp(-t / .06))
    s = (np.sin(w) + .4 * np.sin(2 * w) + .15 * np.sin(3 * w)) * np.exp(-t / .35)
    s += gurultu(1.8, 80, 900) * np.exp(-t / .09) * .9
    s += gurultu(1.8, 1500, 6000) * np.exp(-t / .008) * .2
    return kenar(tepeye_gore(s), son=.2)


def kalp():
    """Müziğin nabzı: yumuşak, derin bir davul; tokmağı keçeli."""
    t = zaman(.32)
    w = faz(44 + 70 * np.exp(-t / .03))
    s = (np.sin(w) + .3 * np.sin(2 * w)) * np.exp(-t / .12) + gurultu(.32, 300, 1500) * np.exp(-t / .006) * .12
    return kenar(tepeye_gore(s), bas=.002)


def calka():
    """Çalkalama (shaker): taneler bir anda değil, 8 ms içinde yığılır; bu yüzden tıs değil hışır diye duyulur."""
    t = zaman(.11)
    return kenar(gurultu(.11, 4500, 10000) * (1 - np.exp(-t / .008)) * np.exp(-t / .03), son=.03)


def alt_bas(f, sure, vurus_suresi, nabizli=True):
    """Alt bas: uzun tutulan kök nota. nabizli: her vuruşta kısılıp geri açılır (davula yer açar, müziğe soluk verir).
    Küçük hoparlörlerde de duyulsun diye üst katları da vardır."""
    t = zaman(sure)
    w = 2 * np.pi * f * t
    s = np.sin(w) + .3 * np.sin(2 * w) + .12 * np.sin(3 * w)
    if nabizli:
        s *= 1 - .75 * np.exp(-(t % vurus_suresi) / .11)
    return kenar(s, bas=.02, son=min(.6, sure / 3))


MERKEZLER = np.arange(np.log2(110), np.log2(15000), .5)   # süpürmenin bantları (log2 Hz), yarım oktav arayla


def supur(lf, zarf, genislik=.7):
    """Frekansı zamanla kayan gürültü (hışırtı). lf: her örnek için merkez frekansın log2'si; zarf: ses yüksekliği.
    Gürültü yarım oktavlık bantlara ayrılır; her bant, merkez frekansa yakınlığı kadar açılır. Çift kanal."""
    n = len(lf)
    f = np.log2(np.maximum(np.fft.rfftfreq(n, 1 / SR), 1.0))
    agirliklar = [np.exp(-.5 * ((lf - m) / genislik) ** 2) for m in MERKEZLER]
    toplam = np.sqrt(sum(w * w for w in agirliklar))
    cikti = np.zeros((n, 2))
    for kanal in range(2):
        X = np.fft.rfft(rng.standard_normal(n))
        for m, w in zip(MERKEZLER, agirliklar):
            bant = np.fft.irfft(X * np.exp(-.5 * ((f - m) / .3) ** 2), n)
            cikti[:, kanal] += bant / np.sqrt(np.mean(bant * bant)) * w
    return kenar(cikti * (zarf / toplam)[:, None])


def hisirti(sure, frekanslar, tepe=.5, genislik=.7):
    """Geçiş hışırtısı. frekanslar: süpürmenin sırayla uğradığı frekanslar (Hz); tepe: sesin en yüksek olduğu an (0-1)."""
    n = ornek(sure)
    u = np.arange(n) / n
    lf = np.interp(u, np.linspace(0, 1, len(frekanslar)), np.log2(frekanslar))
    v = np.where(u < tepe, u / tepe / 2, .5 + (u - tepe) / (1 - tepe) / 2)
    return supur(lf, np.sin(np.pi * v) ** 2, genislik)


def fis():
    """Yazı girerken duyulan kısa fısıltı."""
    return hisirti(.28, [1200, 3600], tepe=.45, genislik=.6)


def yukselis(sure):
    """Vuruştan önce kabaran hava: tizleşerek yükselen, vuruş anında kesilen hışırtı. Çift kanal."""
    u = np.arange(ornek(sure)) / ornek(sure)
    return kenar(supur(np.log2(250) + u * np.log2(5000 / 250), u ** 2.4, .8), son=.006)


def ters_kabarma(tonlar, sure):
    """Vuruştan önceki kabarma: akorun yankılı piyano sesi tersten çalınır; sessizlikten doğup vuruşa doğru büyür.
    Çift kanal."""
    n = ornek(sure)
    kuru = np.zeros((n, 2))
    for i, f in enumerate(tonlar):
        p = piyano(f, .7)[:n]
        kuru[: len(p)] += np.outer(p, (1, .6) if i % 2 else (.6, 1))
    u = np.arange(n) / n
    return kenar(tepeye_gore((kuru * .3 + evristir(kuru, IZ))[::-1] * (u ** 1.5)[:, None]), son=.004)


PARILTI = notalar("D6 F#6 A6 D7 E7")
ALTIN = notalar("E6 A6 C#7 E7")   # sponsor sahnesi: la akoru


def pirilti(sure, tonlar=PARILTI, adet=5):
    """Işıltı: soldan sağa serpilen birkaç çan tınısı ve hafif bir hava sesi. Çift kanal."""
    cikti = np.zeros((ornek(sure + 2.5), 2))
    for k in range(adet):
        u = (k + .2 + .6 * rng.random()) / adet
        s = cubuk(rng.choice(tonlar)) * db(-6 * rng.random())
        i = int(u * sure * SR)
        s = s[: len(cikti) - i]
        aci = (u * 1.2 + .4) * np.pi / 4
        cikti[i:i + len(s)] += np.outer(s, [np.cos(aci), np.sin(aci)])
    u = np.arange(ornek(sure)) / ornek(sure)
    hava = supur(np.log2(4500) + u * np.log2(9000 / 4500), np.sin(np.pi * u) ** 2, .6) * .12
    cikti[: len(hava)] += hava
    return kenar(cikti, son=.3)


def ped(tonlar, sure, atak=.5, birakma=1.0, ust=1100):
    """Ped: yaylı topluluğu gibi uzun, sıcak bir zemin. Her nota, akordu ağır ağır gezinen üçer sesle çalınır;
    sesler ayrı ayrı kabarıp çekilir, bu yüzden zemin org gibi donuk durmaz. Çift kanal."""
    n = ornek(sure + birakma)
    t = np.arange(n) / SR
    cikti = np.zeros((n, 2))
    for f in tonlar:
        for kanal in range(2):
            for _ in range(3):
                sent = rng.uniform(-8, 8) + 4 * np.sin(2 * np.pi * rng.uniform(.1, .35) * t + 6.28 * rng.random())
                w = faz(f * 2 ** (sent / 1200)) + 6.28 * rng.random()
                dalga = sum(np.sin(k * w) / k for k in range(1, 7))
                cikti[:, kanal] += dalga * (1 + .25 * np.sin(2 * np.pi * rng.uniform(.15, .5) * t + 6.28 * rng.random()))
    cikti = suz(cikti, 70, ust)
    cikti /= np.sqrt(np.mean(cikti * cikti))
    return cikti * (gecis(t / atak) * gecis((sure + birakma - t) / birakma))[:, None]


# ================= MİKSER =================

class Miks:
    """Sesleri zaman çizgisine yerleştirir. Her sesin bir de yankıya giden payı olabilir."""

    def __init__(self, sure):
        self.n = ornek(sure)
        self.kuru = np.zeros((self.n, 2))
        self.yanki = np.zeros((self.n, 2))

    def ekle(self, ses, an, kazanc=0.0, pan=0.0, yanki=None):
        """an: başladığı saniye; kazanc: dB; pan: -1 sol .. 1 sağ (tek kanallı sesler için);
        yanki: yankıya giden payın dB'si (None: yankısız)."""
        i = int(round(an * SR))
        if i < 0:
            ses, i = ses[-i:], 0
        ses = ses[: max(0, self.n - i)]
        if not len(ses):
            return
        if ses.ndim == 1:
            aci = (pan + 1) * np.pi / 4
            ses = np.outer(ses, [np.cos(aci), np.sin(aci)]) * np.sqrt(2)
        ses = ses * db(kazanc)
        self.kuru[i:i + len(ses)] += ses
        if yanki is not None:
            self.yanki[i:i + len(ses)] += ses * db(yanki)

    def akor(self, tonlar, an, kazanc, yanki=-6):
        """Piyano akoru: notalar pesten tize, elin yayılışı gibi 10-20 ms arayla basılır; her nota biraz farklı sertlikte."""
        for i, f in enumerate(tonlar):
            self.ekle(piyano(f, .5 + .2 * rng.random()), an, kazanc - 1.5 * rng.random(), pan=(i / max(1, len(tonlar) - 1) - .5) * .5, yanki=yanki)
            an += .01 + .01 * rng.random()


def ton_dengesi(x):
    """Son ton dengesi: en pesler biraz kısılır (küçük hoparlörde duyulmaz, yalnızca yer kaplar), tizler biraz
    açılır (netlik ve ferahlık)."""
    n = len(x)
    lf = np.log2(np.maximum(np.fft.rfftfreq(n, 1 / SR), 1.0))
    kazanc = -2.5 * gecis((np.log2(140) - lf) / 1.5 + .5) + 3.5 * gecis((lf - np.log2(3500)) / 2 + .5)
    return np.fft.irfft((np.fft.rfft(x, axis=0).T * db(kazanc)).T, n, axis=0)


def sinirla(x, tavan):
    """Tepe sınırlayıcı: tavanı aşacak yerlerde sesi 10 ms önceden yumuşakça kısar, sonra yavaşça (0,15 sn)
    geri açar; sesi kırpmaz. Yavaş açılma, bas vuruşlarının her dalgasında kazancın oynamasını önler."""
    gerek = np.minimum(1, tavan / np.maximum(np.abs(x).max(axis=1), 1e-9))
    w = ornek(.01) | 1
    enaz = np.lib.stride_tricks.sliding_window_view(np.pad(gerek, w // 2, mode="edge"), w).min(axis=1)
    tutulan, k, g = np.empty_like(enaz), np.exp(-1 / (.15 * SR)), 1.0
    for i, e in enumerate(enaz.tolist()):
        g = min(e, 1 - (1 - g) * k)
        tutulan[i] = g
    kazanc = np.convolve(np.pad(tutulan, w // 2, mode="edge"), np.ones(w) / w, mode="valid")
    return x * kazanc[:, None]


# ================= NOTASIZ SESLER: klavye, hışırtı, vuruş =================

def efektleri_kur(m, o):
    """Görüntüdeki olayların notasız sesleri. Gecikmeler video.html'deki çizim fonksiyonlarındaki değerlerdir."""
    Z, enterlar = o["Z"], o["enterlar"]

    # --- Açılış kapağı: ortadan ikiye ayrılır (kapakCiz)
    m.ekle(vurus(), .12, -15, yanki=-8)
    m.ekle(hisirti(.8, [300, 1500, 5000], tepe=.4), .06, -17, yanki=-10)

    # --- Soru: imleçle yazılır, Enter'la bir noktaya toplanır (soruCiz)
    for an in seyrelt(o["soru"], .07):
        m.ekle(tus(kalin=.95 + .25 * rng.random()), an, -14 - 3 * rng.random(), pan=.3 * rng.random() - .15, yanki=-16)
    m.ekle(tus(kalin=.6, derin=True), Z["enter"], -10, yanki=-12)
    m.ekle(yukselis(.8), Z["baslik"] - .8, -22, yanki=-10)

    # --- Başlık: logo fırlar ve dolar, harfler sırayla girer (logoCiz, baslikCiz)
    m.ekle(vurus(), Z["baslik"], -7, yanki=-8)
    m.ekle(hisirti(.5, [900, 3200], tepe=.55), Z["baslik"] + .3, -26, yanki=-10)   # LOGOYU KODLA harfleri
    m.ekle(fis(), Z["baslik"] + .75, -28, yanki=-12)

    # --- Oynanış (oyunCiz)
    m.ekle(hisirti(.65, [2400, 900, 400], tepe=.35), Z["oyun"] - .08, -20, yanki=-10)   # logo boşalır, arayüz girer
    for an in seyrelt([x for x in o["tuslar"] if x not in enterlar], .085):
        m.ekle(tus(kalin=.85 + .3 * rng.random()), an, -14 - 4 * rng.random(), pan=.24 * rng.random() - .12, yanki=-18)
    for an in enterlar:
        m.ekle(tus(kalin=.6, derin=True), an, -11, yanki=-13)
    m.ekle(hisirti(.8, [500, 2400, 700]), enterlar[5], -20, yanki=-10)     # logo bir tur döner

    # --- Bitiş: son satırla logo tamamlanır
    m.ekle(yukselis(1.0), enterlar[-1] - 1.0, -21, yanki=-10)
    m.ekle(vurus(), enterlar[-1], -5, yanki=-7)

    # --- Kart (kartCiz)
    m.ekle(hisirti(.7, [500, 3600, 900], tepe=.4), Z["kart"] - .05, -18, yanki=-10)
    m.ekle(fis(), Z["kart"] + .42, -28, yanki=-12)

    # --- Sponsor: kart yan dönüp kaybolur, plaka dönerek gelir (sponsorCiz)
    m.ekle(hisirti(.95, [600, 5000, 800], tepe=.42), Z["sponsor"] - .4, -17, yanki=-10)
    m.ekle(vurus(), Z["sponsor"], -9, yanki=-7)
    m.ekle(fis(), Z["sponsor"] + .45, -28, yanki=-12)

    # --- Kapanış (sonCiz)
    m.ekle(hisirti(.8, [2800, 1200, 500], tepe=.3), Z["son"] - .1, -20, yanki=-10)   # plaka küçülüp aşağı iner
    m.ekle(vurus(), Z["son"] - .06, -12, yanki=-7)
    m.ekle(fis(), Z["son"] + .3, -28, yanki=-12)
    for i in range(3):                                                     # site, Instagram, LinkedIn satırları
        m.ekle(tik(1.2 + .1 * i), Z["son"] + .85 + i * .12, -22, pan=-.2, yanki=-10)


# ================= MÜZİK VE NOTALI SESLER =================

AKORLAR = {   # bas notası, piyano dizilişi, ped notaları, marimba notaları
    "D": ("D2", "D3 A3 E4 F#4 A4", "D3 A3 D4 F#4", "A4 D5 E5 F#5 A5 D6"),
    "Bm": ("B1", "B2 F#3 A3 D4 F#4", "B2 F#3 B3 D4", "B4 D5 F#5 A5 B5 D6"),
    "G": ("G2", "G2 D3 A3 B3 F#4", "G2 D3 G3 B3", "B4 D5 G5 A5 B5 D6"),
    "Asus": ("A2", "A2 E3 A3 D4 E4", "A2 E3 A3 D4", "A4 D5 E5 A5 D6 E6"),   # askıda la: bitişteki reye çözülür
    "A": ("A2", "A2 E3 B3 C#4 E4", "A2 E3 A3 C#4", "A4 C#5 E5 A5 B5 E6"),
}
OSTINATO = (0, 2, 3, 2, 4, 2, 3, 5)                       # dört vuruşluk marimba kalıbı (nota sıraları, sekizlikler)
ONAY = notalar("A4 B4 D5 E5 F#5 A5 B5 D6")                # satır onayları bu dizide tırmanır (re majör pentatonik)


def muzigi_kur(m, o):
    """Müzik: 120 vuruş/dk, re majör. Vuruşlar görüntüye bağlıdır: logonun çıkışı 0. vuruş, logonun tamamlanması
    17. vuruştur; kart, sponsor ve kapanış sahneleri de vuruşlara denk gelir.
    Oynanışta müzik seyrektir (nabız, bas, akorlar): ritmi klavye, ezgiyi satır onayları verir. Logo tamamlanınca
    marimba ve çalkalama katılır; kapanışta yalnızca piyano kalır."""
    Z, enterlar = o["Z"], o["enterlar"]
    vs = (Z["bitis"] - Z["baslik"]) / 17
    an = lambda v: Z["baslik"] + v * vs
    insan = lambda: .005 * rng.standard_normal()   # elle çalınmış gibi: birkaç milisaniyelik oynama
    dizi = [(0, "D"), (4, "Bm"), (8, "G"), (12, "Asus"), (17, "D"), (21, "G"), (25, "A"), (29, "G"), (31, "D")]
    akor = lambda v: AKORLAR[[ad for bas, ad in dizi if bas <= v][-1]]

    # Ped: girişte loş, üçlüsüz bir re; sonra akorlar
    m.ekle(ped(notalar("D3 A3 D4"), Z["baslik"] - .3, atak=1.2, birakma=.6, ust=700), .15, -24, yanki=-10)
    for (bas, ad), (bit, _) in zip(dizi, dizi[1:] + [((o["sure"] - Z["baslik"]) / vs, None)]):
        m.ekle(ped(notalar(AKORLAR[ad][2]), (bit - bas) * vs), an(bas), -27, yanki=-8)

    # Nabız ve alt bas: oynanışta (4-16) ve kart ile sponsor sahnelerinde (18-31)
    for v in list(range(4, 16)) + list(range(18, 31)):
        m.ekle(kalp(), an(v), -14.5)
    for bas, bit in ((4, 8), (8, 12), (12, 16), (17, 21), (21, 25), (25, 29), (29, 31)):
        m.ekle(alt_bas(nota(akor(bas)[0]), (bit - bas) * vs, vs), an(bas), -20)
    m.ekle(alt_bas(nota("D2"), 4 * vs, vs, nabizli=False), an(31), -22)

    # Piyano: sahne açılışlarında dolu, aralarda yumuşak akorlar
    m.akor(notalar("D3 A3 E4 F#4 A4 D5"), Z["baslik"], -13)
    for v, guc in ((4, -19), (8, -19), (12, -19), (21, -18), (29, -19)):
        m.akor(notalar(akor(v)[1]), an(v) + insan(), guc)
    m.akor(notalar("D2 A2 D3 A3 E4 F#4 A4 D5"), enterlar[-1], -12, yanki=-4)
    m.akor(notalar("A2 E3 B3 C#4 E4 A4"), Z["sponsor"], -14, yanki=-4)
    m.akor(notalar("D2 A2 D3 F#3 A3 E4"), Z["son"] - .06, -14, yanki=-4)
    for i, ad in enumerate(("A4", "D5", "F#5", "A5", "D6")):        # kapanış: ağır ağır tırmanan re akoru
        m.ekle(piyano(nota(ad), .5), an(32 + i) + insan(), -20 - i, pan=(-.2, .2)[i % 2], yanki=-3)

    # Vuruşlardan önce tersten kabaran piyano
    for akor_notalari, vurus_ani, sure, guc in (("D3 A3 D4 F#4 A4", Z["baslik"], 1.3, -14), ("D3 A3 D4 F#4 A4 D5", enterlar[-1], 1.6, -13),
                                                ("A2 E3 A3 C#4 E4", Z["sponsor"], .9, -17), ("D3 A3 D4 F#4", Z["son"] - .06, .9, -18)):
        m.ekle(ters_kabarma(notalar(akor_notalari), sure), vurus_ani - sure, guc)

    # Satır onayları: her Enter'da bir marimba notası; satırlar ilerledikçe tizleşir, sonuncusu büyük akordur
    for i, enter in enumerate(enterlar[:-1]):
        m.ekle(marimba(ONAY[i], .8), enter + .012, -14, pan=(-.15, .15)[i % 2], yanki=-7)

    # Bitişe tırmanış (12-16. vuruşlar): marimba ve çalkalama alttan gelip yükselir
    for v in np.arange(12, 16, .5):
        yukselme = (v - 12) / 4
        m.ekle(marimba(notalar(AKORLAR["Asus"][3])[OSTINATO[int((v - 12) * 2)]], .4 + .3 * yukselme), an(v) + insan(),
               -31 + 9 * yukselme, pan=(-.3, .3)[int(v * 2) % 2], yanki=-6)
        m.ekle(calka(), an(v) + insan(), -35 + 9 * yukselme, pan=.2)

    # Kart ve sponsor sahneleri (18-31. vuruşlar): marimba kalıbı ve çalkalama
    for v in np.arange(18, 31, .5):
        vurusta = v % 1 == 0
        m.ekle(marimba(notalar(akor(v)[3])[OSTINATO[int(((v - 17) % 4) * 2)]], .45 + .3 * rng.random()), an(v) + insan(),
               -24 - 2 * rng.random() + (1.5 if vurusta else 0), pan=(-.3, .3)[int(v * 2) % 2], yanki=-6)
        m.ekle(calka(), an(v) + insan(), -30 if vurusta else -26, pan=.2)

    # Görüntüdeki notalı olaylar
    m.ekle(pirilti(.7, adet=4), Z["baslik"] + .62, -22, yanki=-3)                 # logo dolunca parlar
    m.ekle(pirilti(.6, adet=3), enterlar[3] + .03, -24, yanki=-3)                 # renk gelir
    m.ekle(pirilti(1.0, adet=6), enterlar[-1] + .03, -19, yanki=-3)               # logo tamamlanır
    for gecikme, guc in ((.75, -23), (2.05, -25)):                                # kartın üstünden geçen ışıltı
        m.ekle(pirilti(.95, adet=4), Z["kart"] + gecikme, guc, yanki=-3)
    for i, ad in enumerate(("A5", "B5", "D6", "F#6")):                            # unvanlar sırayla yanar
        m.ekle(marimba(nota(ad), .8), Z["kart"] + 1.1 + i * .22, -18, pan=-.3 + i * .2, yanki=-6)
    m.ekle(cubuk(nota("F#6")), Z["kart"] + 1.1 + 3 * .22, -23, pan=.3, yanki=-3)  # Senior altına döner
    m.ekle(cubuk(nota("E6")), Z["sponsor"] + .02, -19, yanki=-3)                  # sponsor plakası yerine oturur
    for gecikme, guc in ((.7, -23), (1.9, -25)):                                  # plakanın üstünden geçen ışıltı
        m.ekle(pirilti(.9, ALTIN, adet=4), Z["sponsor"] + gecikme, guc, yanki=-3)
    m.ekle(pirilti(.95, adet=4), Z["son"] + 1.3, -24, yanki=-3)                   # "klavyeni konuştur" yazısının ışıltısı


# ================= SESLENDİRME =================

# Seslendirme kaynakları: dosya adı -> (telefon kaydı mı, cümlelerin dosyadaki başlangıç ve bitişleri (sn)).
# Saniyeler o dosya için ölçülmüştür; dosya yenilenirse yeniden ölçülmelidir. Cümlelerin içine dokunulmaz.
# Telefon kayıtları ayrıca temizlenir (bkz. konusmayi_isle); stüdyo kalitesindeki hazır seslere dokunulmaz.
SESLENDIRMELER = {
    # ElevenLabs, "Miraç - Energetic Corporate Advertising" sesi, Eleven v4, Türkçe; aynı metnin iki okuması
    "seslendirme-elevenlabs-1.mp3": (False, ((0.05, 1.61), (1.87, 3.02), (3.27, 4.58), (4.77, 7.92), (8.15, 10.14), (10.32, 12.38), (12.58, 14.94))),
    "seslendirme-elevenlabs-2.mp3": (False, ((0.05, 1.56), (1.85, 2.97), (3.27, 4.57), (4.77, 7.91), (8.13, 10.18), (10.41, 12.47), (12.67, 14.96))),
    # Kendi sesiyle okunan kayıt (WhatsApp sesli mesajı). 6. cümlede 12.18'den sonrası mikrofona üflenen nefes.
    "seslendirme-kayit.mp4": (True, ((0.72, 2.27), (2.63, 3.48), (3.96, 5.08), (5.26, 7.94), (8.47, 10.13), (10.64, 12.21), (12.57, 14.46))),
}
SESLENDIRME = "seslendirme-elevenlabs-1.mp3"   # --seslendirme verilmezse kullanılan
# Cümlelerin videoda başladığı anlar: her biri, ekranda yazısının belirdiği sahnenin başına bağlıdır
CUMLE_YERLERI = (
    lambda Z: Z["soru"] - .12,                      # Kaç saniyede kod yazarsın?
    lambda Z: Z["baslik"] + .45,                    # Logoyu Kodla.
    lambda Z: Z["oyun"] + .40,                      # Kod bilmene gerek yok.
    lambda Z: (Z["yaz"] + Z["bitis"]) / 2 - .40,    # Ekrandaki kodu yaz, logo satır satır oluşsun.
    lambda Z: Z["kart"] + .55,                      # Kartını al, story'de paylaş.
    lambda Z: Z["sponsor"] + .50,                   # İlk üçe Goldenfingers'tan menü.
    lambda Z: Z["son"] + .40,                       # Standımıza gel, klavyeni konuştur.
)
KISMA_DB = -7.5          # konuşma sırasında müzik ve efektler bu kadar kısılır
KONUSMA_USTU_DB = 3.0    # konuşma, müziğin (kısılmadan önceki) olağan düzeyinin bu kadar üstünde durur
KONUSMA_TEPE_DB = 13.0   # konuşmanın tepeleri ortalamasının en çok bu kadar üstüne çıkar (sivri heceler yumuşatılır)


def kaydi_oku(dosya):
    """Ses dosyasını tek kanal, 48 kHz olarak okur."""
    veri = subprocess.run([ffmpeg_yolu(), "-loglevel", "error", "-i", str(KLASOR / dosya), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                          capture_output=True, check=True).stdout
    return np.frombuffer(veri, "<f4").astype(float)


def duzey(x, pencere=.01):
    """Sesin kısa süreli düzeyi (dB), örnek örnek: 'pencere' saniyelik ortalama güçten."""
    w = ornek(pencere)
    return 10 * np.log10(np.convolve(x * x, np.ones(w) / w, mode="same") + 1e-12)


def bant_duzeyleri(x, konusulan):
    """Konuşulan yerlerde 1, 2 ve 4 kHz çevresinin, sesin gövdesine (300-600 Hz) göre düzeyi (dB)."""
    X = np.abs(np.fft.rfft(x[konusulan])) ** 2
    f = np.fft.rfftfreq(int(konusulan.sum()), 1 / SR)
    guc = lambda alt, ust: 10 * np.log10(X[(f >= alt) & (f < ust)].sum() / (np.log2(ust / alt)))   # oktav başına
    return tuple(guc(m / 1.26, m * 1.26) - guc(300, 600) for m in (1000, 2000, 4000))


def yumusat(hedefler, inis, cikis):
    """Kazanç eğrisini yumuşatır (1 ms'lik adımlarla): kazanç 'inis' ms'de iner, 'cikis' ms'de geri çıkar."""
    cikti, g = np.empty_like(hedefler), 0.0
    for i, hedef in enumerate(hedefler.tolist()):
        g += (hedef - g) * (1 - np.exp(-1 / (inis if hedef < g else cikis)))
        cikti[i] = g
    return cikti


def konusmayi_isle(x):
    """Telefon kaydını seslendirmeye hazırlar; sesi olabildiğince az değiştirir (fazla işlem sesi yapaylaştırır).
    Kayıt boğuk: 600 Hz - 4 kHz arası (sözün açıklığını taşıyan bölge) olağan bir konuşmaya göre 10-14 dB zayıf,
    160-500 Hz arası baskın. Yapılanlar: 120 Hz altı kesilir (uğultu, mikrofona vuran hava), pes-orta 2 dB kısılır,
    zayıf bölge geniş ve yumuşak bir eğriyle 4,5 dB açılır (eksikliğin üçte biri kadar); s, ş, ç sesleri en çok
    4 dB bastırılır; yüksek heceler 2'ye 1 oranında hafifçe bastırılır. Tıkalı burnun tınısı işlemle giderilemez
    (m ve n seslerinin genizden çıkmaması söyleyişle ilgilidir); bunun için kaydın yenilenmesi gerekir."""
    n = len(x)
    d = duzey(x)
    konusulan = d > np.percentile(d, 90) - 25
    once = bant_duzeyleri(x, konusulan)
    lf = np.log2(np.maximum(np.fft.rfftfreq(n, 1 / SR), 1.0))
    can_egrisi = lambda merkez, genislik: np.exp(-.5 * ((lf - np.log2(merkez)) / genislik) ** 2)
    ton = db(-2 * can_egrisi(300, .9) + 4.5 * can_egrisi(1800, 1.3)) * gecis((lf - np.log2(120)) / .4 + .5)
    x = np.fft.irfft(np.fft.rfft(x) * ton, n)
    sonra = bant_duzeyleri(x, konusulan)
    print("seslendirme tonu (gövdeye göre 1 / 2 / 4 kHz): önce %.0f / %.0f / %.0f dB, sonra %.0f / %.0f / %.0f dB" % (once + sonra))

    # Islık bastırıcı: tiz bant, konuşmanın yüksek düzeyinin 10 dB altını aştıkça kısılır (en çok 4 dB)
    yuksek = np.percentile(duzey(x), 90)
    tiz = suz(x, 4500, None, egim=.5)
    islik = -np.clip((duzey(tiz, .005) - (yuksek - 10)) * .5, 0, 4)
    x = x - tiz + tiz * db(islik)

    # Sıkıştırma: 2'ye 1; düzey 1 ms'lik adımlarla izlenir, kazanç 8 ms'de iner, 150 ms'de geri gelir
    adim = SR // 1000
    kazanc = yumusat(-np.maximum(0, duzey(x)[::adim] - (yuksek - 6)) * (1 - 1 / 2), 8, 150)
    x = x * db(np.interp(np.arange(n), np.arange(len(kazanc)) * adim, kazanc))
    print("seslendirme: ıslık bastırma en çok %.1f dB, sıkıştırma en çok %.1f dB" % (-islik.min(), -kazanc.min()))
    return x


def seslendirmeyi_kur(o, dosya):
    """Cümleleri dosyadan keser, düzeylerini eşitler ve videodaki yerlerine koyar. Tek kanal; konuşulan yerlerde
    ortalama gücü 1 olacak şekilde döner (asıl düzeyi miks belirler)."""
    telefon, kesimler = SESLENDIRMELER[dosya]
    kayit = kaydi_oku(dosya)
    if telefon:
        kayit = konusmayi_isle(kayit)
    cikti = np.zeros(ornek(o["sure"]))
    print("seslendirme: " + dosya)
    for (bas, bit), yer in zip(kesimler, CUMLE_YERLERI):
        parca = kenar(kayit[ornek(bas):ornek(bit)], bas=.015, son=.07)
        d = duzey(parca)
        parca = parca / np.sqrt(np.mean(parca[d > np.percentile(d, 90) - 25] ** 2))   # her cümle aynı yükseklikte
        i = ornek(yer(o["Z"]))
        cikti[i:i + len(parca)] += parca[: len(cikti) - i]
        print("  %5.2f - %5.2f sn: dosyadaki %.2f-%.2f" % (i / SR, (i + len(parca)) / SR, bas, bit))
    return cikti


def seslendirmeyi_ekle(yatak, konusma):
    """Konuşmayı müziğin üstüne oturtur: konuşma sırasında müzik ve efektler KISMA_DB kadar kısılır (konuşmadan
    0,2 sn önce inmeye başlar, bitince çabucak geri açılır ki cümle aralarındaki vuruşlar tam duyulsun); konuşma, müziğin
    olağan düzeyinin KONUSMA_USTU_DB üstünde durur ve yalnızca en sivri tepeleri yumuşatılır. Sese yankı eklenmez."""
    konusuluyor = duzey(konusma, .02) > -35
    blok = SR // 100                                   # kısma eğrisi 10 ms'lik adımlarla kurulur
    k = konusuluyor[::blok].astype(float)
    genis = np.array([k[max(0, i - 12):i + 21].max() for i in range(len(k))])   # 0,2 sn önce başlar, 0,12 sn sonra biter
    kisma = np.convolve(np.pad(genis, 10, mode="edge"), np.ones(21) / 21, mode="valid")
    kisma = np.interp(np.arange(len(yatak)), np.arange(len(kisma)) * blok, kisma)
    # Müziğin olağan düzeyi: yarım saniyelik dilimlerin ortancası (vuruşlar ve sessiz giriş sonucu oynatmasın)
    dilim = SR // 2
    guc = (yatak[: len(yatak) // dilim * dilim] ** 2).sum(axis=1).reshape(-1, dilim).mean(axis=1)
    olagan = np.sqrt(np.median(guc))
    yatak = yatak * db(KISMA_DB * kisma)[:, None]
    konusma = konusma * olagan * db(KONUSMA_USTU_DB)
    konusma = sinirla(konusma[:, None], olagan * db(KONUSMA_USTU_DB + KONUSMA_TEPE_DB))[:, 0]
    print("seslendirme: konuşma sırasında müzik %.1f dB kısılıyor; konuşma, müziğin olağan düzeyinin %.1f dB üstünde" % (-KISMA_DB, KONUSMA_USTU_DB))
    return yatak + konusma[:, None]


# ================= ÜRETİM =================

def ffmpeg_yolu():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def olaylari_oku():
    """Olayların saniyelerini video.html'den okur."""
    with sync_playwright() as p:
        tarayici = p.chromium.launch(channel="msedge", headless=True)
        sayfa = tarayici.new_page()
        sayfa.goto((KLASOR / "video.html").as_uri() + "?uretim")
        sayfa.wait_for_function("window.hazir === true")
        o = sayfa.evaluate("({ sure: SURE, Z, tuslar, enterlar: CALISMA, soru: soruHarfleri.slice(0, -1).map((h) => h.an) })")
        tarayici.close()
    return o


def wav_yaz(yol, x):
    """16 bit, çift kanal; yuvarlama gürültüsü titreşimle (dither) örtülür."""
    veri = np.rint(np.clip(x, -1, 1) * 32766 + rng.random(x.shape) - rng.random(x.shape)).astype("<i2")
    with wave.open(str(yol), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(veri.tobytes())


def olc(yol):
    """Dosyanın ses yüksekliğini (LUFS) ve gerçek tepesini (dBTP) ffmpeg'in EBU R128 ölçeriyle ölçer."""
    cikti = subprocess.run([ffmpeg_yolu(), "-hide_banner", "-nostats", "-i", str(yol), "-af", "ebur128=peak=true", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    ozet = cikti[cikti.rindex("Summary:"):]
    return float(re.search(r"I:\s+(-?[\d.]+) LUFS", ozet).group(1)), float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", ozet).group(1))


def sesi_uret(o):
    efekt = Miks(o["sure"])
    efektleri_kur(efekt, o)
    kuru, yanki = efekt.kuru, efekt.yanki
    if not a.muziksiz:
        muzik = Miks(o["sure"])
        muzigi_kur(muzik, o)
        kuru, yanki = kuru + muzik.kuru, yanki + muzik.yanki
    # Yankıya giden sesin pesleri kesilir (pes yankı uğultu yapar); 28 Hz altı hiç duyulmaz, yalnızca yer kaplar
    x = ton_dengesi(suz(kuru + evristir(suz(yanki, 220, None), IZ), 28, None))
    dosya = a.seslendirme or SESLENDIRME
    if dosya != "yok":
        x = seslendirmeyi_ekle(x, seslendirmeyi_kur(o, dosya))
    t = np.arange(len(x)) / SR
    x *= (gecis(t / .02) * gecis((o["sure"] - t) / .9))[:, None]   # sonda 0,9 saniyede söner

    # Ses yüksekliği: hedefe getirilir; tepeler sınırlayıcıyla en çok EN_COK_SINIRLAMA_DB kadar bastırılır
    wav_yaz(WAV, x / max(1, np.abs(x).max()))
    yukseklik, _ = olc(WAV)
    yukseklik += 20 * np.log10(max(1, np.abs(x).max()))   # ölçüm için küçültüldüyse geri ekle
    kazanc = min(HEDEF_LUFS - yukseklik, TAVAN_DB - 20 * np.log10(np.abs(x).max()) + EN_COK_SINIRLAMA_DB)
    x = sinirla(x * db(kazanc), db(TAVAN_DB))
    wav_yaz(WAV, x)
    yukseklik, tepe = olc(WAV)
    print("tamam: %s (%.1f sn, %.1f LUFS, tepe %.1f dBTP)" % (WAV.name, len(x) / SR, yukseklik, tepe))


def videoya_ekle(video):
    """Sesi videoya ekler. Görüntü akışı aynen kopyalanır; videoda ses varsa yenisiyle değişir."""
    gecici = video.with_name(video.stem + ".gecici.mp4")
    sonuc = subprocess.run([ffmpeg_yolu(), "-y", "-loglevel", "error", "-i", str(video), "-i", str(WAV),
                            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                            "-movflags", "+faststart", str(gecici)])
    if sonuc.returncode:
        gecici.unlink(missing_ok=True)
        sys.exit("ses eklenemedi: " + video.name)
    gecici.replace(video)
    print("tamam: %s (%.1f MB)" % (video.name, video.stat().st_size / 1e6))


sesi_uret(olaylari_oku())
if not a.yalniz_ses:
    videolar = [pathlib.Path(d) for d in a.dosya] if a.dosya else [
        v for v in (KLASOR / "logoyu-kodla-tanitim.mp4", KLASOR / "logoyu-kodla-tanitim-4k.mp4") if v.exists()]
    for video in videolar:
        videoya_ekle(video)
