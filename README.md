# Otonom Kurumsal Finans Ajani

Sirket kurallarini RAG ile hafizasina alan, gelen harcama taleplerine insan
mudahalesi olmadan **ONAY / RED** karari veren ve onayladigi odemeyi **Solana**
uzerinde USDC olarak gerceklestiren otonom ajan.

```
Talep metni
   |
   v
[1] RAG Retrieval ....... Qdrant + FastEmbed (yerel, multilingual)
   |
   v
[2] Karar Motoru ........ Gemini 2.5 Flash, structured output (AgentDecision)
   |                      + prompt-injection savunmasi
   v
[3] Politika Katmani .... LLM'den BAGIMSIZ, kod ile zorunlu limit kontrolu
   |
   v
[4] Solana Transfer ..... USDC (SPL) / SOL  ->  TxHash
   |
   v
[5] SQL Log ............. karar + gerekce + TxHash (SQLite / Postgres)
```

## Mimari ilke: model karar verir, kod izin verir

Bir dil modeli ikna edilebilir; bir `if` blogu edilemez. Bu yuzden ajanin
"ONAY" demesi parayi hareket ettirmeye **yetmez**. Karar `app/agent/policy.py`
icindeki deterministik kontrollerden de gecmek zorundadir:

| Kontrol | Nerede |
|---|---|
| Base58 cuzdan adresi gecerliligi (Pubkey ile dogrulama) | `policy.enforce` |
| Tek islem tavani (`MAX_SINGLE_PAYMENT_USDC`) | `policy.enforce` |
| 24 saatlik kumulatif harcama tavani (SQL'den okunur) | `policy.enforce` + `db.spent_last_24h` |
| Cuzdan beyaz listesi (`ALLOWLISTED_WALLETS`) | `policy.enforce` |
| Prompt injection on filtresi (LLM'e gitmeden) | `prompts.prefilter_injection` |

Ayrica her hata yolu **RED** ile biter: model erisilemezse, sema uretemezse veya
beklenmeyen bir istisna olusursa ajan "acik duserek" onay vermez.

## Kurulum

```bash
conda create -y -n agentco python=3.12 && conda activate agentco
pip install -r requirements.txt
cp .env.example .env          # GEMINI_API_KEY'i doldurun
```

`.env` icinde en az `GEMINI_API_KEY` tanimli olmalidir (https://aistudio.google.com/apikey). Diger her sey
varsayilanlarla calisir: Qdrant yerel dosyaya yazar, log SQLite'a duser ve
`DRY_RUN=true` oldugu icin zincire hicbir sey yazilmaz.

## Calistirma

```bash
# 1) Ornek kural dokumanini hafizaya yukle (istege bagli; arayuzden de yuklenebilir)
python -m scripts.seed

# 2) Backend
uvicorn app.main:app --reload

# 3) Demo arayuzu (ayri terminal)
streamlit run ui/streamlit_app.py
```

Arayuz `http://localhost:8501`, API dokumantasyonu `http://localhost:8000/docs`.

## API uclari

| Metot | Uc | Aciklama |
|---|---|---|
| `POST` | `/memory/upload` | PDF/TXT/MD kural dokumani yukler ve vektorler |
| `GET` | `/memory/search?q=` | Retrieval katmanini tek basina test eder |
| `GET` | `/memory/stats` | Hafizadaki parca sayisi |
| `DELETE`| `/memory` | Kurumsal hafizayi sifirlar |
| `POST` | `/agent/request` | **Ana uc**: talep -> karar -> odeme -> log |
| `GET` | `/operations` | Tum karar ve islem loglari |
| `GET` | `/wallet` | Cuzdan bakiyesi ve aktif limitler (gizli anahtar donmez) |

```bash
curl -X POST localhost:8000/agent/request -H 'content-type: application/json' -d '{
  "text": "Ahmet Yilmaz'\''in 500 USDC'\''lik donanim faturasini onayla. Cuzdan: 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
  "requester": "finans@acme.com"
}'
```

## Zincire gercek odeme yapmak

Varsayilan `DRY_RUN=true`'dur; TxHash `SIMULATED-...` doner. Gercek transfer icin:

```bash
# Devnet operasyon cuzdani uret
python -c "from solders.keypair import Keypair; k=Keypair(); print('ADRES:',k.pubkey()); print('SECRET:',k)"

# .env
AGENT_WALLET_SECRET=<yukaridaki SECRET>
DRY_RUN=false
```

Cuzdani islem ucreti icin SOL ile (`solana airdrop 2 <ADRES> --url devnet`) ve
devnet USDC ile fonlayin (https://faucet.circle.com). Odeme yapilirken alicinin
token hesabi yoksa ajan ayni islemde idempotent olarak olusturur.

> Mainnet'e gecerken `SOLANA_CLUSTER=mainnet-beta`, `USDC_MINT` degerini mainnet
> USDC adresiyle degistirin ve `ALLOWLISTED_WALLETS` listesini mutlaka doldurun.

## Prompt injection savunmasi

Uc katmanlidir:

1. **On filtre** (`prefilter_injection`) — bariz kaliplar modele hic gitmeden kesilir.
2. **Sistem promptu** — talep metni `<talep>` blogu icinde "guvenilmeyen veri"
   olarak isaretlenir; kurumsal kurallar tek yetki kaynagidir. Model injection
   gorurse `injection_detected=true` isaretler.
3. **Politika katmani** — `injection_detected` gelirse odeme her halukarda durdurulur.

Arayuzdeki "Prompt injection - engellenmeli" senaryosu bunu canli gosterir.

## Guvenlik: repoya ne girer, ne girmez

Bu proje bir **odeme cuzdaninin gizli anahtarini** tutar. `.gitignore` buna gore
yazilmistir; asagidakiler hicbir kosulda commit edilmemelidir:

| Girmez | Neden |
|---|---|
| `.env` | `GEMINI_API_KEY` ve `AGENT_WALLET_SECRET` burada |
| `*.key`, `id.json`, `keypair*.json`, `wallet*.json` | `solana-keygen` ciktilari — cuzdani ele gecirir |
| `.streamlit/secrets.toml` | Streamlit sir dosyasi |
| `storage/` | Qdrant vektorleri + SQLite operasyon loglari (makinede uretilir) |
| `local_cache/` | FastEmbed'in indirdigi ONNX modelleri (yuzlerce MB) |

`.env.example` **bilerek** takip edilir (`!.env.example` kurali) — icinde sir yok,
sadece anahtar isimleri var.

> Gizli anahtar bir kez commit edildiyse `.gitignore` eklemek yetmez: anahtar
> git gecmisinde kalir. O cuzdani bosaltip yenisini uretin.

## Proje yapisi

```
app/
  config.py            ortam degiskeni tabanli ayarlar
  schemas.py           tum veri sozlesmeleri (AgentDecision, PaymentInstruction, ...)
  db.py                operasyon loglari + 24s harcama sorgusu
  rag/store.py         chunking, embedding, Qdrant upsert/retrieval
  agent/prompts.py     sistem promptu + injection on filtresi
  agent/decision.py    Gemini structured output ile karar uretimi
  agent/policy.py      LLM'den bagimsiz guvenlik katmani
  agent/graph.py       LangGraph dongusu (retrieve -> decide -> policy -> execute -> log)
  chain/wallet.py      Keypair, SPL/SOL transferi, TxHash dogrulama
  main.py              FastAPI
ui/streamlit_app.py    tek sayfalik demo arayuzu
data/                  ornek kurumsal politika dokumani
scripts/seed.py        ornek dokumani hafizaya yukler
storage/               Qdrant dosyalari + SQLite (git'e girmez, otomatik olusur)
.env.example           ayar sablonu — kopyalayip .env yapin
```

## Model notu

Karar motoru **Gemini 2.5 Flash** kullanir (`google-genai` SDK). Cikti semasi
Pydantic ile zorlanir (`response_schema=AgentDecision`), boylece cuzdan adresi
ve tutar serbest metinden regex ile ayiklanmaz — modelden tipli gelir.

`temperature=0.0` ayarlanmistir: ayni talep + ayni kural seti her zaman ayni
karari uretir. Bu bir denetlenebilirlik gereksinimidir, stil tercihi degil.

`thinking_budget=-1` ile dinamik dusunme aciktir; model karmasik taleplerde daha
uzun dusunur. `.env` uzerinden `THINKING_BUDGET=0` yapilarak kapatilabilir
(daha hizli/ucuz, daha zayif karar).

Guvenli taraf davranisi: model guvenlik filtresi istemi engellerse
(`prompt_feedback.block_reason`), uretim yarida kesilirse (`finish_reason != STOP`)
veya sema uretilemezse karar otomatik **RED** olur.

## Sorun giderme

| Belirti | Sebep / cozum |
|---|---|
| Her talep RED, gerekce "No API key was provided" | `.env` icinde `GEMINI_API_KEY` bos. https://aistudio.google.com/apikey |
| "Kurumsal hafizada bu talebe dair kural bulunamadi" | Hafiza bos — `python -m scripts.seed` calistirin veya arayuzden dokuman yukleyin |
| Ilk calistirma cok uzun suruyor | FastEmbed embedding modelini bir kez indiriyor (~220 MB). Sonraki acilislar hizli |
| TxHash `SIMULATED-...` donuyor | `DRY_RUN=true` (varsayilan). Gercek transfer icin yukaridaki bolume bakin |
| "Attempt to debit an account but found no record of a prior credit" | Operasyon cuzdaninda SOL yok. `solana airdrop 2 <ADRES> --url devnet` |
| Karar ONAY ama odeme yapilmadi | Politika katmani durdurmustur — `policy.violations` alanina bakin |
| `EMBEDDING_MODEL` degistirdim, retrieval bozuldu | Vektor boyutu degisti. `DELETE /memory` ile koleksiyonu sifirlayip yeniden yukleyin |
