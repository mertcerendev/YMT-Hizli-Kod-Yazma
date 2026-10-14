# Logoyu Kodla

BANÜ Yazılım Mühendisliği Topluluğu (YMT) stant oyunu. Tek dosya: `index.html`; internet olmadan da çalışır.

## Başka bir bilgisayarda açmak

1. Bu sayfada **Code → Download ZIP** ile indir ve ZIP'i bir klasöre çıkar.
2. `index.html`'i Chrome ya da Edge ile aç, **F11** ile tam ekran yap.
3. Giriş ekranındayken **F2** yönetim panelini açar (PIN: `index.html`'deki `ADMIN_PIN`).
4. Depodaki `index.html`'de `EPOSTA_SERVISI` ve `EPOSTA_ANAHTARI` güvenlik için boştur; boşken oyun çalışır ama kart e-postası ve Google tablosuna yazma kapalıdır. İkisini doldurmak için ymtbanu hesabında Apps Script'teki **"Logoyu Kodla e-posta"** projesini aç:
   - `EPOSTA_SERVISI`: **Dağıt → Dağıtımları yönet**'teki web uygulaması adresi.
   - `EPOSTA_ANAHTARI`: koddaki `ANAHTAR` satırının değeri.

## Oyuncu kayıtları bu depoda yok

Kayıtlar oyunun açıldığı tarayıcıda tutulur; depodan indirilen oyun boş sıralamayla başlar. Kayıtları taşımak için:

- Eski bilgisayarda yönetim panelinden **Yedek al**, yeni bilgisayarda **Yedekten yükle**.
- Bilgisayar tamamen gittiyse: her oyun ymtbanu hesabının Drive'ındaki **"Logoyu Kodla kayıtları"** Google tablosuna da yazılır.

## Klasörler

- `tanitim-videosu/`: tanıtım videosu (1080p ve 4K), sahnesi (`video.html`) ve onu üreten betikler (`uret.py`, `ses.py`).
- `eposta-servisi-kurulum.txt`: kart e-postası ve Google tablosu servisinin (Google Apps Script) kurulumu ve kodu.
- `index-*.html`: oyunun eski sürümleri ve tasarım denemeleri.
- `onizleme/`: ekran görüntüleri.
