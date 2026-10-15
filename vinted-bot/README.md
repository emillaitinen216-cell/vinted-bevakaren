# Vinted-bevakaren

En bot som bevakar Vinted och skickar en notis till mobilen när ett
märkesplagg dyker upp så billigt att det går att sälja vidare med vinst.

Färdiga sökningar finns för:

- **Herrjeans:** Nudie Jeans, Replay, Dondup och Diesel, plus en bootcut-sökning
  (Levi's 517, Diesel Zatiny, True Religion, Evisu …)
- **Fältjackor:** hela kategorin *Militär- och bruksjackor* (Stone Island,
  C.P. Company, Barbour, Ralph Lauren, Carhartt, Alpha Industries …), Barbour
  och "field jacket"/M65
- **Dunjackor:** Moncler, Canada Goose, Arc'teryx, Parajumpers, Fjällräven,
  The North Face, Patagonia, Peak Performance och Moose Knuckles
- **Jackor och rockar:** Stone Island, C.P. Company, Burberry, Prada,
  Arc'teryx, Herno, Ralph Lauren, Woolrich, Napapijri, Sail Racing, Haglöfs,
  Norrøna, Mammut, Patagonia och Carhartt
- **Tröjor, hoodies och skjortor:** Moncler, Supreme, Acne Studios,
  C.P. Company, Ami Paris, Stone Island, Burberry, Ralph Lauren,
  Fear of God Essentials och Our Legacy
- **Byxor, shorts och fler jeans:** C.P. Company, Stone Island, Jacob Cohën,
  A.P.C. och Acne Studios

För varje annons räknar boten ut en ungefärlig vinst:

```
säljvärde = resale × skickfaktor + plus för nyckelord (t.ex. "selvage", "Zatiny")
kostnad   = pris inkl. Vinteds köparskydd + frakt
vinst     = säljvärde − kostnad
```

Du får en notis när vinsten är minst `min_profit` för sökningen. Så här ser en
notis ut:

```
🔥 Nudie Jeans – 120 kr (≈ +298 kr)
   Nudie Grim Tim selvage W32
   💰 120 kr · 134 kr inkl. köparskydd + ~49 kr frakt
   📈 Säljvärde ≈ 480 kr → vinst ≈ 298 kr (163%)
   📏 48 | W32 · Mycket bra
   ✨ selvage (+100)
   🔎 Nudie Jeans
   🏷 #Nudie #Jeans
```

Varje notis får också hashtaggar för märke och kategori, till exempel
`#Dondup #Jeans` eller `#Moncler #Dunjacka`. Trycker du på en hashtagg i
Telegram ser du alla notiser med samma tagg, så att du kan bläddra märke för
märke. Kategorierna ställs in med `tags` i `config.yaml`.

Märken som ofta kopieras (Moncler, Canada Goose, Stone Island, TNF …) får
**⚠️ Misstänkt lågt pris** när priset är under `suspicious_below`. Kontrollera
äktheten innan du köper.

---

## Snabbstart

Du behöver Python 3.10 eller senare.

```bash
cd vinted-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # Windows: copy .env.example .env
# fyll i en notiskanal i .env (se nedan)

python vinted_bot.py --test-notis   # kolla att notiserna kommer fram
python vinted_bot.py --dry-run      # visa vad som skulle bli notis just nu
python vinted_bot.py                # starta bevakningen
```

Första gången en sökning körs sparas de annonser som redan ligger ute, utan
notis. Efter det får du bara notiser om **nya** annonser och om **prissänkningar**
som gör en annons lönsam. Vill du ha notiser även för de befintliga annonserna
sätter du `notify_on_first_run: true`.

Varje sökning bedömer annonserna med sina egna priser. En Arc'teryx-dunjacka
syns till exempel både i *Arc'teryx jackor* och *Arc'teryx dun*, och blir
notis om någon av dem tycker att den är lönsam. Samma annons skickas ändå bara
en gång.

### Kommandon

| Kommando | Vad det gör |
|---|---|
| `python vinted_bot.py` | Bevakar i en loop, ett varv var fjärde minut |
| `--dry-run` | Ett varv där allt som matchar skrivs ut. Inget skickas och inget sparas |
| `--kalibrera` | Visar median och kvartiler för annonspriserna per märke |
| `--telegram` | Kopplar Telegram: kontrollerar token, hittar ditt chat-id och sparar båda i `.env` |
| `--test-notis` | Skickar en testnotis till alla kanaler du har ställt in |
| `--once` | Kör ett varv och avslutar (bra för cron) |
| `--bara TEXT` | Kör bara sökningar vars namn innehåller TEXT, t.ex. `--bara moncler` |
| `-v` | Visar varför varje annons sorterades bort |
| `--paus MIN MAX` | Sekunder att vänta mellan anropen till Vinted, t.ex. `--paus 0.5 1.5` (standard 3–7) |
| `--max-tid SEK` | Börjar inga nya sökningar efter SEK sekunder. Nästa körning fortsätter där den slutade |
| `-c FIL` | Använder en annan konfigurationsfil |

---

## Notiser

Ställ in en eller flera kanaler i `.env`. Det går också i `config.yaml`, men
`.env` hålls utanför git så att dina nycklar inte hamnar på GitHub.

### Telegram (rekommenderas)

1. Öppna Telegram och skriv till **@BotFather**: `/newbot`. Välj ett namn och
   ett användarnamn som slutar på `bot`. Kopiera **token** du får. Den ser ut
   ungefär så här: `123456789:AAHk3vZx9QeR7tYuIoPaSdFgHjKlZxCvBn0`.
2. Sök upp din nya bot i Telegram, tryck **Starta** och skriv ett meddelande,
   till exempel *hej*.
3. Kör:
   ```bash
   python vinted_bot.py --telegram
   ```
   Klistra in token när du blir tillfrågad. Kommandot kontrollerar token,
   hittar ditt chat-id, sparar båda i `.env` och skickar ett testmeddelande.

Notiserna kommer med bild och en knapp, **Öppna på Vinted**.

Dela aldrig din token, till exempel i en chatt eller på GitHub. Den som har
den kan styra din bot. Har den läckt skickar du `/revoke` till @BotFather och
får en ny.

#### Skicka fynden till en grupp

Vill du dela fynden med kompisar, eller hålla dem skilda från din privata
chatt, kan boten posta i en grupp:

1. Välj **Ny grupp** i Telegram. Lägg till din bot genom att söka på dess
   användarnamn (`@…bot`), plus de vänner du vill ha med, och skapa gruppen.
2. Klart. Nästa gång boten kör, inom en halvtimme i molnet, märker den att den
   har lagts till. Den skickar då *"Vinted-bevakaren är kopplad hit!"* i
   gruppen, och efter det kommer alla fynd dit.

Skriv `/start` till boten i er privata chatt för att få fynden dit igen. Tas
boten bort ur gruppen går fynden tillbaka till din privata chatt. Görs gruppen
om till en supergrupp följer boten med automatiskt.

Bara du, som först skrev till boten, kan flytta fynden. Andra som trycker
*Starta* på boten, skriver `/start` i gruppen eller lägger till den i sina egna
grupper påverkar ingenting.

**Måste boten vara admin?**

- I en vanlig grupp där alla medlemmar får skriva räcker det att boten är
  vanlig medlem. Det är också bäst: då får den bara kommandon och händelser
  som "tillagd" och "borttagen", inte allt ni skriver.
- I en **kanal**, eller i en grupp där medlemmar inte får skriva, måste boten
  vara **administratör** med rätten att skicka (publicera) meddelanden. Tryck
  på gruppens namn, sedan pennan, sedan *Administratörer* och *Lägg till
  administratör*.
- Får boten inte skriva i gruppen skickar den fynden till din privata chatt i
  stället, tillsammans med ett meddelande om vad som behöver ändras. Inga fynd
  försvinner. Det som inte går att skicka alls sparas och skickas vid nästa
  körning, i upp till ett dygn.

Bytet sker bara när `TELEGRAM_CHAT_ID` inte är satt; ett angivet chat-id gäller
alltid. Telegram sparar händelserna i ett dygn, så boten måste hinna köra inom
den tiden.

<details>
<summary>Hitta chat-id för hand i stället</summary>

Öppna länken nedan i webbläsaren, med din token direkt efter `bot`. Ta bort
`<` och `>` och lägg inga mellanslag:

```
https://api.telegram.org/bot123456789:AAHk3vZx9QeR7tYuIoPaSdFgHjKlZxCvBn0/getUpdates
```

| Svar | Betyder |
|---|---|
| `"chat":{"id":987654321,…` | Talet är ditt chat-id |
| `{"ok":true,"result":[]}` | Token är rätt men boten har inte fått något meddelande än. Skriv till den och ladda om sidan |
| `404 Not Found` | Länken är fel: `<` `>` är kvar, det finns ett mellanslag eller `bot` saknas före token |
| `401 Unauthorized` | Token är felkopierad. Hämta den igen hos @BotFather (`/mybots` → din bot → *API Token*) |

Lägg sedan in båda i `.env`:

```
TELEGRAM_BOT_TOKEN=123456789:AAHk3vZx9QeR7tYuIoPaSdFgHjKlZxCvBn0
TELEGRAM_CHAT_ID=987654321
```

</details>

### ntfy (enklast, inget konto)

Installera appen [ntfy](https://ntfy.sh) och prenumerera på ett ämne med ett
svårgissat namn, t.ex. `vinted-fynd-k3j9x2`. Lägg sedan in samma namn i `.env`:

```
NTFY_TOPIC=vinted-fynd-k3j9x2
```

Alla som känner till ämnesnamnet kan läsa notiserna, så hitta inte på något
lättgissat.

### Discord

Gå till *Serverinställningar › Integrationer › Webhooks › Ny webhook*,
kopiera webhook-URL:en och lägg in den i `.env`:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/…
```

---

## Anpassa sökningarna

Alla sökningar finns i `config.yaml`. Ett exempel:

```yaml
- name: Diesel
  query: diesel          # söktext på Vinted
  catalog: [257]         # Herr › Jeans
  max_price: 250         # hämta annonser upp till 250 kr
  min_profit: 120        # notis om uppskattad vinst ≥ 120 kr
  brands:
    diesel: 260          # vad du räknar med att kunna sälja för
  premium_keywords:
    zatiny: 300          # Zatiny är värd 300 kr mer
```

- **brands** anger vilka märken som bevakas och vad de brukar säljas för. Ett
  märke kan också ha egna gränser:
  `stone island: {resale: 2000, max_price: 1100, suspicious_below: 500}`.
  Märkesnamnen matchas ord för ord, utan hänsyn till skiftläge, accenter och
  apostrofer. `levis` matchar alltså "Levi's", `fjallraven` matchar
  "Fjällräven" och `ralph lauren` matchar "Polo Ralph Lauren".
- **resale** på själva sökningen gäller för alla märken som inte står i `brands`.
- **min_price** hoppar över annonser som är billigare än så. Det är användbart
  för Moncler och Canada Goose, där de allra billigaste nästan alltid är kopior.
- **sizes:** `[W32, W33, M]` begränsar sökningen till vissa storlekar.
- **premium_keywords** höjer säljvärdet när ett ord finns i titeln. Negativa
  värden sänker det.
- **exclude_keywords** hoppar över annonser vars titel innehåller ordet.
- **tags:** `[Jeans]` ger hashtaggen `#Jeans` i notisen. Märket blir alltid en hashtagg.
- **enabled: false** pausar en sökning.

Globala inställningar högst upp i filen:

| Inställning | Standard | Betydelse |
|---|---|---|
| `interval_seconds` | 240 | Tid mellan varven (± 20 % slump) |
| `shipping_estimate` | 49 | Frakten du betalar som köpare |
| `sell_fee_percent` | 0 | Avgift när du säljer. Vinted tar ingen, Tradera ungefär 10 % |
| `min_roi_percent` | 30 | Minsta avkastning på insatsen |
| `condition_multipliers` | | Skickfaktorer, t.ex. Bra = 0,85 och Ny med prislapp = 1,25 |

### Kalibrera säljvärdena

```bash
python vinted_bot.py --kalibrera
```

```
Moncler
   moncler   n=191 median  3 519 kr  (mitten-50 %: 2 000–5 500)   resale i config: 3 200 …
```

Det här är **annonspriser**. Det som faktiskt säljs går ofta 10–20 % under.
En bra tumregel är att välja `resale` och `min_profit` så att ett vanligt plagg
blir notis först när priset ligger bland den billigaste fjärdedelen av
annonserna, alltså det undre talet i "mitten-50 %". Annonser som är ännu
billigare har ofta en anledning: slitage, fel modell eller en kopia.

Värdena i `config.yaml` är kalibrerade mot vinted.se i oktober 2026.

### Kategori-ID:n (Herr)

| ID | Kategori |
|---|---|
| 257 | Jeans |
| 1206 | Kappor & jackor (allt) |
| 2051 | Kappor |
| 2052 | Jackor (alla underkategorier) |
| 2535 | Militär- och bruksjackor (fält-/M65-jackor) |
| 2536 | Dunjackor |
| 2537 | Quiltade jackor |
| 1223 | Bomberjackor |

Andra kategorier: öppna kategorin på vinted.se. Siffran i adressen
(`/catalog/2536-puffer-jackets`) är ID:t.

---

## Kör i molnet utan egen dator (GitHub Actions)

Det här passar om du bara har en webbläsare, till exempel en skol-Chromebook.
GitHub kör då boten gratis var tionde minut, även när din dator är avstängd.

1. Skapa boten hos @BotFather (steg 1 under [Telegram](#telegram-rekommenderas)).
2. Öppna repot på GitHub och gå till *Settings › Secrets and variables ›
   Actions › New repository secret*. Fyll i:
   - **Name:** `TELEGRAM_BOT_TOKEN`
   - **Secret:** din token från @BotFather

   Tryck **Add secret**. Chat-id behövs inte, boten hittar det själv.
3. Skriv *hej* till boten i en privat chatt. Då blir du dess ägare och fynden
   kommer dit. Ska de i stället till en grupp skriver du sedan `/vinted` i
   gruppen (se [Skicka fynden till en grupp](#skicka-fynden-till-en-grupp)).
   Gör det högst ett dygn före första körningen; äldre meddelanden har
   Telegram redan glömt.
4. Vill du testa direkt går du till fliken **Actions**, väljer
   **Vinted-bevakaren** och trycker **Run workflow**. Första körningen sparar
   annonserna som redan ligger ute och skickar *"Vinted-bevakaren är kopplad
   hit!"* till Telegram. Efter det kommer notiser om nya fynd.

Bra att veta:

- **Repot ska vara publikt.** Då är Actions-minuterna obegränsade och boten
  kan köra var tionde minut. Koden syns för alla, men din token ligger som
  hemlighet och syns aldrig. Boten körs i tyst läge (`--tyst`), så loggarna
  visar inga fynd, chattnamn eller chatt-id:n.
- **Privat repo:** ett gratiskonto har bara 2 000 Actions-minuter i månaden
  för privata repon, delade mellan alla dina privata repon, och varje påbörjad
  minut räknas. Kör då högst varannan halvtimme: ändra `cron` i
  `.github/workflows/vinted-bot.yml` till `"7,37 * * * *"` och kommandot till
  `python vinted_bot.py --once --paus 0.5 1.5 --max-tid 30`. Tar minuterna slut
  stoppas alla Actions-körningar på kontot till nästa månad.
- **Varje körning** söker i högst sju minuter (`--max-tid 420`). Sökningar som
  inte hann med tas först i nästa körning.
- **Förseningar:** GitHub startar ibland schemalagda körningar några minuter
  för sent, och vid hög belastning hoppas en del körningar över.
- **60 dagar:** schemalagda körningar i publika repon stängs av automatiskt
  efter 60 dagar utan ändringar i repot. GitHub skickar ett mejl innan. Tryck
  då **Enable workflow** under fliken Actions.
- **Spärrar:** Vinted kan ibland spärra GitHubs servrar. Boten försöker då igen
  vid nästa körning.

## Köra dygnet runt på egen dator

En egen dator kan söka oftare än molnet, var fjärde minut. Kör boten hemifrån,
på en dator som står på eller på en Raspberry Pi. Vinted spärrar ofta anrop
från molnservrar (datacenter-IP), så en vanlig hemuppkoppling fungerar bäst.

**Enklast på Linux, Mac och Chromebook med Linux aktiverat:** kör
`bash starta.sh`. Skriptet installerar det som behövs första gången, kopplar
Telegram och startar bevakningen. Skol-Chromebooks har oftast Linux avstängt,
så använd molnet ovan i stället.

**Linux/Raspberry Pi med systemd.** Skapa `/etc/systemd/system/vinted-bot.service`:

```ini
[Unit]
Description=Vinted-bevakaren
After=network-online.target

[Service]
WorkingDirectory=/home/pi/vinted-bevakaren/vinted-bot
ExecStart=/home/pi/vinted-bevakaren/vinted-bot/.venv/bin/python vinted_bot.py
Restart=always
RestartSec=60
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now vinted-bot
journalctl -u vinted-bot -f      # följ loggen
```

**Windows:** låt ett terminalfönster stå öppet med `python vinted_bot.py`, eller
lägg in den i Schemaläggaren så att den startar när datorn startar.

Sedda annonser och historik över alla notiser sparas i `vinted_bot.db`
(SQLite). Tabellen `deals` går att öppna i t.ex. *DB Browser for SQLite* om du
vill se vilka fynd du har fått.

---

## Hur det fungerar

Vinted har inget öppet API. Boten hämtar samma katalogsida som webbläsaren
(`/catalog?search_text=…&catalog[]=…&price_to=…&order=newest_first`), men med
headern `RSC: 1`. Då svarar Next.js med enbart sidans data, där varje annons
ligger som JSON. Fungerar inte det läses samma data ur HTML-sidan i stället.

Paketet `curl_cffi` används eftersom det härmar Chromes TLS-fingeravtryck.
Vanliga Python-anrop stoppas av Cloudflare. Om Vinted svarar 403 eller 429
försöker boten igen med en ny session och pausar annars en stund, upp till
15 minuter.

Om Vinted ändrar sin sida kan tolkningen sluta fungera. Kör då
`python vinted_bot.py --dry-run --debug-dir debug` och titta i de sparade svaren.

## Bra att veta

- Boten är inofficiell. Automatiserad hämtning kan strida mot Vinteds villkor.
  Använd den för eget bruk och sänk inte intervallet nämnvärt, både för att inte
  belasta Vinted och för att inte bli spärrad.
- Sökningarna på vinted.se visar även annonser från andra länder, till exempel
  Polen och Danmark. Frakten kan då bli dyrare än `shipping_estimate`.
- Säljvärdet är en uppskattning. Titta alltid på bilder, mått och skick innan
  du köper.

## Tester

```bash
python -m unittest -v
```
