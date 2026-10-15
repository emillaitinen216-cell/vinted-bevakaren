# Vinted-bevakaren

En bot som bevakar Vinted och skickar en notis till Telegram när ett
märkesplagg dyker upp så billigt att det går att sälja vidare med vinst:
herrjeans, fältjackor, dunjackor, jackor, tröjor och byxor från ett
fyrtiotal sökningar (Stone Island, C.P. Company, Moncler, Arc'teryx,
Ralph Lauren, Nudie, Diesel …).

Boten kör gratis i molnet med GitHub Actions var tionde minut. Ingen egen
dator behövs.

**Allt om hur den fungerar och hur du ställer in den står i
[vinted-bot/README.md](vinted-bot/README.md).**

| Fil | Vad den gör |
| --- | --- |
| [`vinted-bot/config.yaml`](vinted-bot/config.yaml) | Sökningarna, märkena och priserna |
| [`vinted-bot/vinted_bot.py`](vinted-bot/vinted_bot.py) | Själva boten |
| [`.github/workflows/vinted-bot.yml`](.github/workflows/vinted-bot.yml) | Kör boten var tionde minut |

Boten är inofficiell och har inget med Vinted att göra. Säljvärdena är
uppskattningar. Titta alltid på bilder, mått och skick innan du köper.
