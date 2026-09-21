# Mandate

> **Kurumsal ajan platformu.** Ajanlar şirket adına çalışır; yetkisi sınırlıdır.
> *Mandate*, "sınırları belli, devredilmiş yetki" demektir: bu ürünün yaptığı şey tam olarak budur.

Kurumsal is akislari icin cok ajanli bir isletim sistemi. Tek bir giris noktasina gelen
talebi bir **Router (Supervizor)** uygun uzman ajana yonlendirir; kritik eylemler
**insan onayi** olmadan asla yurutulmez. Model katmani tek bayrakla **bulut (Gemini)**
ile **yerel (Ollama)** arasinda gecis yapar; gizli sirket verisi makineden cikmadan islenebilir.

```
 talep (+ ek dosya, kanal) ─► guard ── injection ─► ENGELLENDI
                                │
                   kanal=customer ──────────────────────────────┐
                                │                                │
                             router ── kapsam disi ─► SON        │
          ┌───────────┬─────────┼──────────┬────────────┬────────┴───────┐
     A: IT & Ops  B: Veri   C: Sozlesme  D: Finans  E: Satin Alma  F: Musteri Destek
     RAG + arac   Text-SQL  RAG (kirmizi SQL butce  RAG (sartname)  triaj + aciliyet
                  (salt     cizgiler)    + limit                    (yalnizca oneri)
                  okunur)                                                 │
          │          └─► SON    │           │           │                 └─► SON
          └─────────────────────┴─────┬─────┴───────────┘
                          eylem onerisi (pending_action)
                          ┌───────────▼───────────┐
                          │ human_approval        │ ◄─ interrupt(): graf BURADA durur
                          └─────┬───────────┬─────┘    API 202 "awaiting_approval"
                          ONAY  │           │ RED
                     execute_action     cancel_action
```

## Tasarim ilkeleri

**Ajanlar oneri yapar, graf yurutur.** Hicbir ajan bir eylemi kendisi calistirmaz; yalnizca
`pending_action` onerir. Eylemin calistigi tek yer `execute_action` dugumudur ve oraya
yalnizca insan onayindan gecilerek ulasilir. "Kritik eylem onaysiz calisti" hatasi
yapisal olarak mumkun degildir.

**Modele guvenilmez, dogrulanir.** Modelin urettigi her sey kodla kontrol edilir:

| Model ciktisi | Kod kontrolu |
|---|---|
| Arac argumanlari | E-posta bicimi, yazilim katalogu (`validate_call`) |
| Arac cagrisinin kendisi | Argumanlar talepte geciyor mu (`ungrounded_args`): bilgi sorusu uydurma eyleme donusemez |
| SQL | Salt-okunur baglanti + SQLite authorizer + zaman asimi + satir tavani + tarih ifadesi denetimi |
| Sozlesme bulgusu | Alinti sozlesmede geciyor mu; kanitsiz ihlal elenir |
| Sozlesme risk seviyesi | Model degil KOD hesaplar (en yuksek bulgu seviyesi) |
| Insan karari | Yalnizca acik `approved: true` onaydir (`"true"`, `1`, bos karar = RED) |

**Fail-closed.** Model erisilemezse, cikti gecersizse, sozlesmenin bir maddesi analiz
edilemezse sonuc "guvenli" sayilmaz: talep hata doner veya insana gider.

## Ajanlar

### Router (Supervizor) — [app/agent/router.py](app/agent/router.py)
Talebi `it_ops`, `data_analyst`, `contract_analyst` veya `unsupported`'a siniflandirir.
Hic eylem yapmaz. Dusuk guvende netlestirme ister.

### A — IT & Ops Destek — [app/agent/it_ops.py](app/agent/it_ops.py)
Qdrant'taki **IT/IK politikalarini** tarar, sorulari cevaplar. Islem taleplerinde arac onerir:
- `reset_password(email)` — **mock**
- `grant_access(software, user)` — **mock**, yalnizca katalogdaki yazilimlar

Talep sahibi ile hedef hesap farkliysa onay kartinda uyari gosterilir.
Araclar [app/tools/it_tools.py](app/tools/it_tools.py) icindedir; gercek bir kimlik sistemine
(Okta, Entra ID) baglamak yalnizca bu dosyayi degistirir.

### B — Veri Analisti — [app/agent/data_analyst.py](app/agent/data_analyst.py)
**RAG kullanmaz.** Dogrudan yerel SQLite'a baglanir: dogal dil → SQL → guvenli calistirma →
deterministik istatistik → Turkce ozet. Hatali SQL'i bir kez kendisi duzeltir.
Salt-okunur oldugu icin onay gerektirmez.

Guvenlik modeli ([app/tools/sales_db.py](app/tools/sales_db.py)) SQL'in dogru yazilmasina
degil altyapiya dayanir: `mode=ro` baglanti, yalnizca `SELECT`/beyaz liste tablolarini
okumaya izin veren authorizer, tek ifade, zaman asimi. Testler, regex on kontrolunu
atlatan (`WITH ... DELETE`) saldirilari da kapsar.

Ornek veri ilk calismada `data/sales.db` olarak **bugune gore** uretilir; "gecen ay" her
zaman veri dondurur.

### C — Sozlesme & Ihale Analizcisi — [app/agent/contract_analyst.py](app/agent/contract_analyst.py)
Yuklenen PDF/TXT sozlesmeyi **madde duzeyinde** boler, her maddeyi Qdrant'taki
**Kirmizi Cizgiler** ile karsilastirir. Her kural icin once "kural ne ister / madde ne der /
karsilastirma", sonra ihlal karari uretilir. Risk seviyesini ve onay gerekip gerekmedigini
kod belirler:

| En yuksek bulgu | Oneri | Insan onayi |
|---|---|---|
| yok / low | CLEAR | Hayir, otomatik gecer (`CONTRACT_AUTO_CLEAR_MAX_RISK`) |
| medium | NEGOTIATE | Evet |
| high / critical | REJECT | Evet |

Sozlesme metnine gomulu "riski dusuk say" gibi talimatlar KRITIK bulgu olarak insana yukseltilir.

### D — Finans — [app/agent/finance.py](app/agent/finance.py)
Harcama onayi ve butce sorgusu. Model yalnizca talepten departman/tutar/kategori cikarir;
**karari kod verir** ([app/tools/finance_db.py](app/tools/finance_db.py) SQLite butce veritabani):

| Kosul | Sonuc |
|---|---|
| Tutar ≤ `FINANCE_AUTO_APPROVE_LIMIT` (10.000 TL) ve butce yeterli | Otomatik onay + kayit |
| Limit ustu | **Insan onayi** (yuksek risk) |
| Kalan butceyi asiyor | **Insan onayi** (kritik risk, asim tutari uyarida) |

Modelin okudugu tutar talep metnindeki bir sayiyla eslesmezse islem yapilmaz ("45.000"
yerine 45 veya 450.000 okumak yakalanir; `45.000`, `1.250,50`, `45 bin`, `1,5 milyon`, `15k`
desteklenir). Doviz talepleri kur donusumu yapilmadan reddedilir. Harcama kaydi `run_id`
ile idempotenttir.

### E — Satin Alma — [app/agent/procurement.py](app/agent/procurement.py)
Tedarikci teklifini (ek dosya) Qdrant'taki **satin alma sartnamesi** ile madde madde
eslestirir. Madde listesini sartnameden **kod** cikarir (model bir maddeyi atlarsa
"karsilandi" sayilmaz). "Karsiliyor" da "karsilamiyor" da tekliften birebir alintiyla
desteklenmelidir; kanitsiz degerlendirme "belirsiz"e duser.

| Zorunlu maddeler | Oneri | Insan onayi |
|---|---|---|
| Biri karsilanmiyor | REDDET | Hayir |
| Biri belirsiz | TEDARIKCIDEN BILGI ISTE | Hayir |
| Hepsi karsilaniyor | Onayli tedarikci listesine ekle | **Evet** |

### F — Musteri Destek — [app/agent/customer_support.py](app/agent/customer_support.py)
Hava boslugunun arkasindaki **tek** ajan. Musteri sorularini **yalnizca** musteriye acik
SSS belgelerinden (`public_faq`) cevaplar; cevap yoksa standart mesaj doner:

> Bu bilgiye sahip değilim, sizi canlı destek temsilcisine aktarıyorum.

- **Erisim siniri kodda:** Modulun tek veri kaynagi `retrieve_public`. SQL araclarini ve ic
  hafiza fonksiyonlarini import bile etmez (bir test bunu kaynak kod uzerinden denetler).
- **Fiziksel olarak ayri koleksiyon:** `public_faq`, ic belgelerle ayni Qdrant koleksiyonunda
  bir filtre arkasinda degil, ayri bir koleksiyonda durur.
- **Halusinasyon imkansiz (varsayilan `SUPPORT_REPLY_MODE=extractive`):** Musteriye modelin
  yazdigi metin degil, secilen SSS maddesinin cevabi **kelimesi kelimesine** gider. Model
  yalnizca "hangi madde" sorusunu cevaplar. Neden: canli testte yerel model "7 gun icinde
  ucretsiz degisim" kuralini "7 gunden sonra ucretli" diye yazdi; sayi kontrolleri bu anlam
  bozulmasini yakalayamaz.
- **Canli destege aktarma kurallari (kodda):** SSS'de cevap yok, model kaynak gostermedi ya
  da yanlis kaynak gosterdi, modelin taslaginda SSS'de olmayan telefon/e-posta/URL/sayi var,
  yasal tehdit / veri ihlali / yuksek aciliyet, model erisilemiyor.
- Triaj (kategori, aciliyet, ic departman, SLA) yalnizca **personele** gorunur; musteriye donmez.

## Hava boslugu: kanal bazli yonlendirme

Her talep bir **kaynaktan** gelir ve her kaynak bir **guven bolgesine** aittir
([app/security.py](app/security.py)):

| Kaynak | Bolge | Ulasabilecegi ajanlar |
|---|---|---|
| `internal_panel`, `internal_slack`, `internal_teams`, `internal_api` | internal | Tum ajanlar (kritik eylemler onay kapisinda) |
| `external_web`, `external_email`, `external_whatsapp` | external | **Yalnizca** Musteri Destek |
| Taninmayan / bos | external | Yalnizca Musteri Destek (fail-closed) |

**Kaynagi istemcinin beyani degil, kimlik bilgisi belirler.** Istek govdesine
`"source": "internal_slack"` yazmak bir saldirgani ic bolgeye sokmaz:

- Ic kaynak yalnizca o kaynaga ait anahtarla (`X-Source-Key`, `.env` -> `INTERNAL_SOURCE_KEYS`)
  kanitlanir. Panel anahtari Slack kaynagini temsil edemez (403).
- Kimligi dogrulanmis bir ic entegrasyon **dis** bir kaynak beyan edebilir (orn. destek
  e-posta kutusunu okuyan entegrasyon -> `external_email`). Yetki yalnizca duser.
- Dis kanal ayri bir uctur: `POST /public/support`. Anahtarsizdir, yalnizca dis kaynak kabul
  eder ve musteriye yalnizca `reference`, `reply`, `handed_off` doner.

Dis bolge kurali **iki bagimsiz katmanda** uygulanir: Router dugumu dis talebi modele hic
sormadan Musteri Destek'e yonlendirir; ardindan grafin kenari, Router bozulsa bile dis
talebin baska bir ajana gecmesini engeller. Testler her katmani tek tek bozarak dogrular.

## Hibrit LLM ve Dinamik Bilissel Yonlendirme

[app/llm/](app/llm/) tum ajanlara tek bir arayuz sunar. Model **ajan basina** secilir:
genel ayar `USE_LOCAL_LLM`, ajan ayari onu ezer.

```bash
USE_LOCAL_LLM=true            # Router, IT & Ops, Veri Analisti -> yerel Ollama
CONTRACT_ANALYST_LLM=cloud    # Sozlesme Analizcisi -> Gemini (genel ayari ezer)
```

| Ajan | Varsayilan yonlendirme | Neden |
|---|---|---|
| Router, IT & Ops, Veri Analisti | `USE_LOCAL_LLM`'i izler | Kisa gorevler; calisan ve satis verisi makineden cikmaz |
| Sozlesme Analizcisi | **bulut** (`CONTRACT_ANALYST_LLM=cloud`) | Olculen: Gemini ~25 sn / precision 1.00, yerel 7B 4-5 dk / ~0.70 |
| Satin Alma | **bulut** (`PROCUREMENT_LLM=cloud`) | Olculen (2 teklif, 20 madde): Gemini 20/20 ~10 sn, yerel 7B 13/20 ~95 sn ve uygun tedarikciyi reddetti |
| Finans, Musteri Destek | `USE_LOCAL_LLM`'i izler (`FINANCE_LLM`, `CUSTOMER_SUPPORT_LLM`) | Karar kodda; model yalnizca cikarim/siniflandirma yapar |

Her ajan icin `<AJAN>_LLM` ayari vardir (`CONTRACT_ANALYST_LLM`, `PROCUREMENT_LLM`,
`FINANCE_LLM`, `CUSTOMER_SUPPORT_LLM`): `cloud` (her zaman Gemini), `local` (her zaman
Ollama), `default` (genel ayari izle). Hangi ajanin hangi modelde calistigi `/health` ->
`llm_routing` ve arayuzun yan panelinde gorunur; her kayit fiilen kullanilan modeli saklar.

> **Gizlilik uyarisi:** `cloud` olan ajanlarin verisi (su an sozlesme metinleri ve tedarikci
> teklifleri), genel mod yerel olsa bile **Google'a gonderilir.** Buluta cikmamasi gereken
> belgeler icin ilgili ayari `local` yapin.
>
> **Fail-closed:** Bulut zorunluyken `GEMINI_API_KEY` yoksa sozlesme analizi saniyeler icinde
> acik bir hatayla durur; sessizce yerel modele **dusmez**.

Yerel katman, arac cagirmayi desteklemeyen modellerde (orn. `phi3`) otomatik olarak JSON
tabanli yedek yola duser; qwen2.5'in arac cagrisini yapisal alan yerine metne yazdigi
durumlari da kurtarir.

> Not: Proje `gemini-2.5-flash` kullanir; `MODEL_ID` ile degistirilebilir.

## Kurulum

```bash
conda create -y -n agentco python=3.12 && conda activate agentco
pip install -r requirements.txt          # gelistirme: requirements-dev.txt
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> .env: ADMIN_API_KEY
# .env: INTERNAL_SOURCE_KEYS=internal_panel:<anahtar>,internal_slack:<anahtar>
# .env: GEMINI_API_KEY (sozlesme analizi icin zorunlu, CONTRACT_ANALYST_LLM=cloud iken)

# yerel model kullanacaksaniz
ollama pull qwen2.5:7b
```

## Calistirma

```bash
python -m scripts.seed --reset           # bilgi tabani + ornek satis DB (sunucu KAPALIYKEN)
uvicorn app.main:app                     # Panel: http://localhost:8000  ·  API dokumantasyonu: /docs
```

## Web paneli

[app/web/](app/web/) altinda, build adimi olmayan bir HTML/CSS/JS paneli; FastAPI ayni
kokenden sunar. Dort ekran: **Talep gonder**, **Onay kuyrugu**, **Islem kayitlari**,
**Bilgi tabani**. Ornek talep kartlari gerekli ornek dosyayi (sozlesme, teklif) otomatik ekler.

- Panel bir ic kaynaktir (`internal_panel`): acilista panel anahtarini ister. Onay kuyrugu ve
  belge yukleme ayrica yonetici anahtari ister (talep eden ile onaylayan ayri kimlikler).
  Anahtarlar yalnizca o tarayici sekmesinde (`sessionStorage`) tutulur; sunucu anahtari
  sayfaya hicbir zaman gomulmez.
- "Musteri mesaji (dis kanal)" modu gercek dis uca (`/public/support`) kimliksiz gider;
  musterinin gordugu dar yaniti ve personelin gordugu ic kaydi yan yana gosterir.
- Model ciktisi ve kullanici metni yalnizca `textContent` ile basilir (`innerHTML` yok) -
  LLM ciktisi gosteren bir panelde XSS'e karsi temel onlem.
- CORS varsayilan olarak kapalidir (panel ayni kokenden). Farkli alan adi gerekiyorsa
  `CORS_ORIGINS`; `*` bilerek kabul edilmez.
- Tasarim token'lari ekteki tasarim sisteminden alindi. Bu sistem perplexity.ai'den
  cikarilmis; lisansli olmayan `pplxSans` yerine **Inter** kullanildi, marka ogesi
  kullanilmadi. Satis/yatirim oncesi kendi marka kimliginize gecmeniz onerilir.

## API ve yetki matrisi

| Yetki | Uclar |
|---|---|
| **Acik** (kimliksiz) | `GET /health` (yalnizca `{"status":"ok"}`), `POST /public/support`, panel dosyalari |
| **Ic** (`X-Source-Key`) | `POST /agent/request`, `POST /agent/request/upload`, `GET /runs`, `GET /runs/{id}`, `GET /system/status`, `GET /memory/stats`, `GET /memory/search` |
| **Yonetici** (`X-Admin-Key`) | `GET /approvals`, `POST /approvals/{id}/decision`, `POST /memory/upload`, `DELETE /memory` |

Anahtar yok -> **401**, yanlis -> **403**, sunucuda anahtar tanimli degil -> **503** (uclar
acik birakilmaz). Bilgi tabanina yazmak yonetici yetkisi ister: bir belgeyi degistirmek
ajanlarin davranisini degistirir (zehirleme riski).

| Metot | Uc | Aciklama |
|---|---|---|
| `POST` | `/public/support` | Musteri mesaji (`message`, `contact`, `source`: yalnizca `external_*`) |
| `POST` | `/agent/request` | Ic talep (`text`, `requester`, istege bagli `source`). Onay gerekirse **202** |
| `POST` | `/agent/request/upload` | Ek dosyali ic talep (form) |
| `GET` | `/approvals` | Onay bekleyen talepler |
| `POST` | `/approvals/{run_id}/decision` | `{"approved": true, "reviewer": "...", "comment": "..."}` |
| `GET` | `/runs`, `/runs/{run_id}` | Denetim kaydi (kaynak ve bolge dahil) |
| `POST` | `/memory/upload?domain=it_hr\|red_lines\|procurement\|public_faq` | Belge yukler |
| `GET` | `/system/status` | Model yonlendirmesi, hafiza, bekleyen onaylar |

```bash
curl -X POST localhost:8000/public/support -H 'content-type: application/json' \
  -d '{"message":"Kac gun icinde iade edebilirim?"}'
# -> {"reference":"...","reply":"Donanım ürünlerini ... 14 gün içinde ...","handed_off":false}

curl -X POST localhost:8000/agent/request -H 'content-type: application/json' \
  -H "X-Source-Key: $PANEL_KEY" \
  -d '{"text":"ali@acme.com hesabimin sifresini sifirlar misin?","requester":"ali@acme.com"}'
# -> 202 {"run_id":"...","status":"awaiting_approval","pending_action":{...}}

curl -X POST localhost:8000/approvals/<run_id>/decision -H 'content-type: application/json' \
  -H "X-Admin-Key: $ADMIN_API_KEY" -d '{"approved":true,"reviewer":"it.mudur@acme.com"}'
# -> 200 {"status":"completed","action_result":{...}}
```

## Kalicilik

| Veri | Yer |
|---|---|
| Bekleyen onaylarin graf durumu | `storage/checkpoints.db` (LangGraph `AsyncSqliteSaver`) |
| Denetim kaydi (talep, yonlendirme, karar, onaylayan, sonuc) | `storage/runs.db` |
| Ic hafiza (3 alan) + musteri SSS (ayri koleksiyon) | `storage/qdrant/` |
| Butce ve harcama kayitlari (ornek) | `data/finance.db` (ilk calismada uretilir) |

Bekleyen bir onay **sunucu yeniden baslasa da kaybolmaz**. Iki inceleyici ayni anda karar
verirse yalnizca biri islenir (atomik durum gecisi); digeri 409 alir.

## Testler

```bash
pip install -r requirements-dev.txt
pytest                                    # 204 test, gercek model GEREKMEZ (sahte LLM)
```

Testler guvenlik ozelliklerini sinar: hava boslugu (dis talep ic ajana ulasamaz, Router
bozulsa bile; yalnizca public koleksiyon sorgulanir; ic belge prompta girmez), onaysiz
yurutme olmamasi, talep sahibinin kendini onaylayamamasi (401/403/503), fail-closed karar, ajan basina model yonlendirmesi, SQL
saldirilari, es zamanli karar, restart sonrasi devam, sozlesmeye gomulu injection.

## Bilinen sinirlar

- **Sozlesme analizi kalitesi iki ornek sozlesmeyle olculdu** (madde duzeyinde, 9 gercek ihlal):

  | Model | Precision | Recall | Sure (12 madde) |
  |---|---|---|---|
  | `gemini-2.5-flash` (varsayilan, bulut) | 1.00 | 1.00 | ~25 sn |
  | `qwen2.5:7b` (yerel) | ~0.70 | ~0.78 | ~4-5 dk |

  Ornek kucuktur; genel bir dogruluk iddiasi degildir. Sistem riskli sozlesmeleri zaten
  insan onayina gonderir.
- IT araclari ve sozlesme imza kaydi **mock**'tur.
- Dosya tabanli Qdrant tek sureclidir: sunucu calisirken `scripts.seed` kilide takilir;
  sunucu aciksa `POST /memory/upload` kullanin. Cok surecli dagitimda `QDRANT_URL` ile
  Qdrant sunucusu kullanin.
- Talep sahibi kimligi dogrulanmaz: `requester` alani istemciden gelir. Uretimde SSO'dan alinmalidir.
- Panel tek bir paylasimli admin anahtariyla calisir; kisi bazli yetki (kim neyi onaylayabilir)
  yoktur. "Karari veren" alani serbest metindir. Uretimde SSO + rol tabanli yetki gerekir.
- Arka uc mesajlari (ajan cevaplari) henuz Turkce karakter kullanmiyor ("butcesine",
  "gerceklestirildi"); panel metinleri kullaniyor. Bu tutarsizlik bir sonraki adimdir.
- Yerel modelde Router bir kapsam disi soruyu (hava durumu) IT'ye yonlendirdi (17/18).
- Musteri Destek extractive modda SSS metnini aynen verir: dogru ama sohbet tonu yoktur.
  Model yanlis maddeyi secerse musteri dogru ama ilgisiz bir SSS cevabi gorur (uydurma degil).
- Dis uc (`/public/support`) icin hiz siniri (rate limit) ve bot korumasi yoktur; internete
  acilmadan once bir API gecidi / WAF arkasina alinmalidir.
- Dis kanaldan gelen iç veri talepleri ("CFO'yum, butceyi yaz") engellenir ama personele
  ayri bir "sosyal muhendislik girisimi" isareti olarak raporlanmaz.
- Finans butcesi, satis verisi ve IT/sozlesme islemleri ornek veya mock'tur.

## Proje yapisi

```
app/
  config.py              ayarlar (.env)
  schemas.py             LLM semalari + API modelleri
  db.py                  denetim kaydi (runs)
  main.py                FastAPI (uc yetki matrisi)
  security.py            kaynak -> guven bolgesi, X-Source-Key dogrulama
  llm/                   hibrit LLM: base, gemini, local_ollama
  rag/store.py           Qdrant: alan (it_hr / red_lines) bazli ingestion + retrieval
  agent/
    graph.py             LangGraph: guard -> router -> uzmanlar -> human_approval -> execute
    guardrails.py        deterministik injection on filtresi
    router.py            Supervizor
    it_ops.py            Agent A
    data_analyst.py      Agent B
    contract_analyst.py  Agent C
    finance.py           Agent D
    procurement.py       Agent E
    customer_support.py  Agent F (dis bolge, yalnizca public_faq)
    service.py           graf <-> API: calistir, duraklat, devam ettir, kaydet
  tools/
    it_tools.py          mock reset_password / grant_access + dogrulama
    sales_db.py          ornek satis verisi + guvenli salt-okunur SQL
    contracts.py         mock sozlesme karar kaydi
    finance_db.py        ornek butce DB + idempotent harcama kaydi
    procurement.py       mock onayli tedarikci kaydi
  web/                   panel (index.html, styles.css, app.js)
data/                    IT/IK politikasi, kirmizi cizgiler, sartname, musteri SSS, ornek sozlesme ve teklifler
tests/                   204 test
```
