# Vinted-bevakaren

En bot som bevakar Vinted och skickar en notis till mobilen när ett
märkesplagg dyker upp så billigt att det går att sälja vidare med vinst.

Färdiga sökningar finns för:

- **Herrjeans:** Nudie Jeans, Replay, Dondup, Diesel, Jacob Cohën, A.P.C., Acne Studios,
  Tramarossa och Edwin, plus bootcut-, flare-, vintage- och Y2K-sökningar (Levi's 517, Diesel
  Zatiny, True Religion, Evisu, Rock Revival, Wrangler, Lee …)
- **Fältjackor:** hela kategorin *Militär- och bruksjackor* (Stone Island,
  C.P. Company, Ralph Lauren, Acne Studios, Barbour, Fjällräven), Barbour och
  "field jacket"/M65
- **Dunjackor:** Moncler, Canada Goose, Arc'teryx, Parajumpers, Fjällräven,
  Patagonia, J.Lindeberg, Colmar och Moose Knuckles
- **Jackor och rockar:** Stone Island, C.P. Company, Burberry, Prada, Arc'teryx,
  Herno, Ralph Lauren, Woolrich, Patagonia, J.Lindeberg,
  Abercrombie & Fitch och Hollister
- **Tröjor, hoodies och skjortor:** Moncler, Acne Studios, C.P. Company, Ami Paris,
  Stone Island, Burberry, Ralph Lauren, J.Lindeberg, Gucci, Balenciaga,
  Abercrombie & Fitch och Hollister

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
   🟢 Säljare & annons: låg risk – 42 omdömen (4,9/5), 9 bilder
   ✨ selvage (+100)
   🔎 Nudie Jeans
   🏷 #Nudie #Jeans
```

Varje notis får också hashtaggar för märke och kategori, till exempel
`#Dondup #Jeans` eller `#Moncler #Dunjacka`. Trycker du på en hashtagg i
Telegram ser du alla notiser med samma tagg, så att du kan bläddra märke för
märke. Kategorierna ställs in med `tags` i `config.yaml`.

Märken som ofta kopieras (Moncler, Canada Goose, Stone Island, Gucci …) får
**⚠️ Misstänkt lågt pris** när priset är under `suspicious_below`. Kontrollera
äktheten innan du köper. Tryck på **🔍 Legitcheck** under ett fynd för att få
en riskbedömning och en checklista för märket (se
[Legitcheck](#legitcheck-är-plagget-äkta)).

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

#### Legitcheck: är plagget äkta?

I fynd-chatten (gruppen) och i din privata chatt med boten kan ni be om en
riskbedömning av en annons:

- Tryck på **🔍 Legitcheck** under ett fynd, eller
- skriv `/legit` och klistra in länken till en Vinted-annons, eller
- svara på ett fynd med `/legit`.

Svaret kommer inom några sekunder:

```
🔍 Legitcheck: Moncler – 1 422 kr
   Risk för kopia: 🔴 Hög
   ⚠️ Varningssignaler
   • Priset (1 422 kr) är under gränsen där kopior är vanliga för Moncler (1 500 kr)
   • Säljaren har inga omdömen
   ✅ Talar för
   • 15 bilder
   📋 Be säljaren om bilder och kolla (Moncler)
   • …
```

Boten väger ihop:

- **Priset** jämfört med vad märket brukar säljas för i samma kategori.
  Jämförelsen gäller bara märken som finns i `config.yaml`.
- **Säljarens omdömen och betyg.**
- **Ord i annonsen** som "replica", "1:1" eller "vet ej om äkta". Ett kvitto
  eller en QR-kod som säljaren nämner räknas som något som talar för.
- **Antal bilder.**

Sedan visar den vad du ska be säljaren fotografera för just det märket, och
länkar till märkets egen äkthetskontroll om det finns en. Märkenas
checklistor står i `legit_tips.yaml` och går att ändra.

**Boten kan inte se på bilderna om ett plagg är äkta.** Det är en
riskbedömning, inte en garanti. Erbjuder Vinted äkthetskontroll för annonsen
står det i svaret. Bara fynd-chatten och privata chattar med medlemmar i gruppen får svar.

- **Är boten inte admin i gruppen** får den bara kommandon som riktas till
  den. Skriv då `/legit@dinbot` (bottens användarnamn), eller gör boten till
  admin.
- **I molnet** svarar boten nästan hela tiden. Det finns en paus på ungefär en
  minut var tionde minut, när en körning byts mot nästa. Kommer svaret inte
  direkt, kommer det efter pausen.
- **På en egen dator** svarar boten så länge `python vinted_bot.py` kör. Kör
  den inte på en egen dator samtidigt som i molnet med samma bot – då tar de
  Telegram-meddelandena från varandra.
- **En enstaka annons utan Telegram:**
  `python vinted_bot.py --legit https://www.vinted.se/items/…`

#### Ämnen, "Jag tar den", sålda fynd och dagens bästa

**Ämnen (en flik per kategori).** Fynden kan sorteras i ämnen som
*Dunjackor*, *Jackor*, *Jeans* och *Tröjor & skjortor*:

1. Tryck på gruppens namn, sedan pennan (*Redigera*), och slå på **Ämnen**.
2. Gör boten till administratör med rätten **Hantera ämnen** (*Administratörer*
   → *Lägg till administratör* → din bot).

Boten skapar ämnena själv vid nästa körning. Vilka sökningar som hamnar i
vilket ämne styrs av `topics` i `config.yaml` (sökningarnas taggar). Utan
Ämnen, eller utan rätten, kommer fynden i gruppen som vanligt. `/legit` som
skrivs i ett ämne besvaras i samma ämne.

**👋 Välkommen och 📖 Start här.** Boten skapar två låsta flikar: en kort
välkomsttext och en guide till gruppens funktioner, kommandon och märken. Texterna
fästs och uppdateras av sig själva när märkena i `config.yaml` ändras.
`info_topics: false` stänger av dem.

**💬 Snack.** En öppen flik där alla kan skriva (`chat_topic` i `config.yaml`).

**💰 /pris, 📝 Säljtext och 🏆 topplistan.**

- `/pris Moncler Maya` visar vad liknande plagg kostar på Vinted just nu: median,
  prisspann och ett förslag på pris för att sälja snabbt.
- Knappen *📝 Säljtext* under ett fynd skickar en färdig annonstext med prisförslag
  privat till den som trycker.
- `/sald 1200` som svar på ett fynd (eller `/sald 1200 400`: såld för, köpt för) räknar
  ut vinsten. `/topplista` visar månadens vinsttopplista, och den 1:a varje månad
  skickas förra månadens topplista.
- Märken vars fynd brukar säljas inom en timme får raden *⚡ Moncler-fynd brukar säljas
  inom 15 min – var snabb*.

**👀 /följ.** `/följ` + länk till en Vinted-annons (eller som svar på ett fynd) ger ett
privat meddelande när priset sänks eller när annonsen blir såld. `/följer` visar
annonserna och `/avfölj 2` eller `/avfölj alla` slutar följa. Varje annons kollas ungefär
var halvtimme.

**🎯 Självjustering.** En gång per dygn höjer boten vinstgränsen för sökningar vars fynd
sällan säljs inom ett dygn, och sänker den lite där allt säljs på minuter (`auto_tune`).

**📊 Statistik.** En låst flik som boten uppdaterar varje dag: vilka märken bland
gruppens fynd de senaste 30 dagarna som säljs snabbast, flest sålda, störst vinst per
sålt fynd och vilka som oftast blir liggande (`stats_topic`).

**📣 /alla (bara för admins).** Telegram har ingen @everyone, och botar får inte se
gruppens medlemslista. `/alla Nya fynd ikväll!` (eller `/alla` som svar på ett
meddelande) skickar därför texten och nämner alla medlemmar boten sett: alla som
skrivit, gått med eller tryckt på en knapp sedan funktionen lades till. Vill du nå
precis alla: fäst meddelandet och välj *Meddela alla medlemmar*.

**🙋 Jag tar den.** Under varje fynd i gruppen finns en knapp. Den som trycker
först får sitt namn på knappen (*"🙋 Emil tar den"*), så att inte två köper
samma plagg. Trycker samma person igen släpps fyndet. Trycker någon annan får
den veta vem som redan tagit det.

**❌ SÅLD inom 15 min.** Boten kollar varje fynd igen efter ungefär 10 och 30
minuter, och sedan efter 1, 3, 6 och 24 timmar. Är plagget sålt ändras inlägget
till *❌ SÅLD inom …* (eller *❌ BORTTAGEN* om annonsen är borta), så att ni
slipper klicka på fynd som redan är borta och ser hur snabbt fynden går.

**🟢🟡🔴 Kopierisk i fynden.** Boten öppnar varje fynd och visar säljarens
omdömen, antalet bilder och varningsord i annonsen direkt i inlägget. Priset
räknas inte in där – att det är lågt vet man redan, och märken som ofta
kopieras har en egen varning. Redan sålda annonser skickas inte alls. Tryck på
*🔍 Legitcheck* för hela bedömningen. Stängs av med `deal_risk: false`.

**🔔 Egna bevakningar.** Alla i gruppen kan få fynd i ett visst märke privat:

- `/bevaka Moncler` – alla Moncler-fynd
- `/bevaka Stone Island L` – bara storlek L (flera storlekar: `M/L`)
- `/bevaka dunjacka` – ord i titeln går också
- `/bevaka Rick Owens max 1500` – vilket märke som helst, även sådana som gruppen inte
  bevakar. Då söker boten efter märket själv, ungefär var 20:e minut, och skickar nya
  annonser privat – aldrig i gruppen – när de kostar högst maxpriset, eller utan maxpris
  när de ligger minst 40 % under vad märket brukar kosta på Vinted just nu.
- `/bevakningar` visar dina bevakningar, `/sluta 2` tar bort nummer 2 och
  `/sluta alla` tar bort alla.

Boten får bara skriva till den som tryckt *Starta* hos den, så det behöver man
göra en gång. Glömmer man det säger boten till, med en knapp att trycka på.
Bevakningarna (`/bevaka`, `/bevakningar`, `/sluta`, `/följ`, `/följer`, `/avfölj`)
skrivs privat till boten, bara för medlemmar i gruppen. Skrivs de i gruppen svarar
boten med en knapp till den privata chatten. Övriga kommandon (`/legit`, `/pris`,
`/sald`, `/topplista`) skrivs i fliken General. Varje person kan ha 10 bevakningar.

**⚡ Toppmärken söks oftare.** Sökningar med `hot: true` (Moncler, Canada Goose,
Parajumpers, Stone Island, C.P. Company jackor, Arc'teryx jackor och Ralph Lauren
tröjor) körs två gånger per varv. I molnet söks de dessutom igen i minuterna som blir
över innan nästa körning, ungefär var 2–3:e minut, eftersom de bästa fynden säljs på
minuter. Då kollas även de privata bevakningarna, ungefär var 10:e minut.

**🌙 Tyst natt.** Fynd mellan kl. 23 och 07 kommer utan ljud. Tiderna ändras
med `quiet_hours` i `config.yaml` (`false` stänger av).

**⭐ Dagens bästa och 📊 veckorapport.** Efter kl. 20 (svensk tid) skickar boten
en gång om dagen dygnets fem fynd med störst vinst som inte hunnit säljas, i ett
eget ämne. På söndagar kommer också veckans siffror: antal fynd, hur många som
såldes och hur snabbt, märket med flest fynd, veckans största fynd och vem som
tagit flest fynd med *🙋 Jag tar den*. Tiden ändras med `daily_summary_hour`
(`false` stänger av dagens bästa), och `weekly_report: false` stänger av
veckorapporten.

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
- **exclude_keywords** hoppar över annonser vars titel innehåller ordet. Den
  globala listan tar redan bort kopior och barnplagg ("barn", "kids", "junior"
  m.fl.). Fraser i **exclude_exceptions** ("barn coat", "kid mohair") räknas inte.
- **womens_keywords** ("dam", "women" m.fl.) hoppar över damplagg, utom när
  titeln också innehåller ett ord från **unisex_keywords** ("herr/dam", "unisex").
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
| `topics` | 5 ämnen | Ämne i Telegram-gruppen per tagg, se [Ämnen](#ämnen-jag-tar-den-sålda-fynd-och-dagens-bästa) |
| `daily_summary_hour` | 20 | När dagens fem bästa fynd skickas (svensk tid). `false` = av |
| `weekly_report` | true | Veckorapport på söndagar |
| `quiet_hours` | [23, 7] | Fynd utan ljud mellan de här timmarna. `false` = av |
| `deal_risk` | true | Kopierisken i varje fynd (ett anrop till hos Vinted per fynd) |

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
Boten körs då gratis på GitHubs servrar var tionde minut, dygnet runt, även när
din dator är avstängd.

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
4. Starta boten: gå till fliken **Actions**, välj **Vinted-bevakaren** och
   tryck **Run workflow**. Första körningen sparar annonserna som redan ligger
   ute och skickar *"Vinted-bevakaren är kopplad hit!"* till Telegram. Efter
   det startar varje körning själv nästa, och du får notiser om nya fynd.

Bra att veta:

- **Repot ska vara publikt.** Då är Actions-minuterna obegränsade och boten
  kan köra var tionde minut. Koden syns för alla, men din token ligger som
  hemlighet och syns aldrig. Boten körs i tyst läge (`--tyst`), så loggarna
  visar inga fynd, chattnamn eller chatt-id:n.
- **Varje körning startar nästa.** GitHubs schema (cron) startar inte på alla
  konton, så när en körning är klar väntar den tills det gått tio minuter och
  startar sedan nästa. Du behöver bara trycka **Run workflow** en gång.
  - **Stoppa boten:** fliken **Actions** › **Vinted-bevakaren** › **⋯** ›
    **Disable workflow**.
  - **Starta igen:** **Enable workflow** och sedan **Run workflow**.
  - Har boten stannat av sig själv (ingen ny körning på en kvart) trycker du
    **Run workflow**.
- **Privat repo:** ett gratiskonto har bara 2 000 Actions-minuter i månaden
  för privata repon, och varje påbörjad minut räknas. Var tionde minut skulle
  göra slut på dem på ett par dygn, och då stoppas alla Actions-körningar på
  kontot till nästa månad. Därför kör workflow-filen inte alls i ett privat
  repo.
- **Varje körning** söker i högst sju minuter (`--max-tid 420`). Sökningar som
  inte hann med tas först i nästa körning.
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
