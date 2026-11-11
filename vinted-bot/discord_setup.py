"""Bygger en Discord-server för resellers: kategorier, kanaler, behörigheter och färdiga texter.

Kör den när du skapat en tom server och bjudit in din Discord-bot (med rätten Administratör):

    DISCORD_BOT_TOKEN=... python discord_setup.py

I GitHub körs den med knappen Actions › Bygg Discord-servern › Run workflow. Den går att köra
igen: kanaler som redan finns lämnas som de är, och texter skrivs bara i nya kanaler.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import vinted_bot as vb

SEND = 1 << 11                     # skicka meddelanden
THREADS = (1 << 35) | (1 << 38)    # skapa trådar och skriva i dem
READ_ONLY = SEND | THREADS


def vip_text(link: str) -> str:
    apply = f"👉 **Ansök här:** {link}" if link else "👉 Fråga en admin hur du går med."
    return "\n".join([
        "💎 **VIP – fynden direkt i mobilen**",
        "",
        "Vår bot söker igenom Vinted dygnet runt och skickar underprisade märkesplagg – Moncler, Stone Island, "
        "Canada Goose, Ralph Lauren, jeans och mycket mer – till vår Telegram-grupp **inom några minuter** efter "
        "att de lagts upp.",
        "",
        "✅ Fynd med uträknad vinst och kopierisk",
        "✅ Egna bevakningar på vilket märke som helst",
        "✅ Prischeck, legitcheck och resell-guide",
        "✅ Topplista och vinster från andra medlemmar",
        "",
        "Se <#sålda-fynd> för vad medlemmarna hann köpa.",
        "",
        apply,
        "När du ansökt skriver boten till dig med pris och hur du betalar. Medlemskapet kostar – priset får du "
        "innan du betalar något.",
    ])


def layout(link: str) -> list[tuple[str, list[tuple[str, str, bool, str]]]]:
    """[(kategori, [(kanal, ämnesrad, bara läsa, text som fästs)])]."""
    return [
        ("📌 INFO", [
            ("välkommen", "Börja här", True, "\n".join([
                "👋 **Välkommen till communityn för resellers!**",
                "",
                "Här delar vi tips, legitchecks och vinster – och hjälper varandra att köpa billigt och sälja med "
                "vinst på Vinted, Tradera och Sellpy.",
                "",
                "📖 Läs <#regler>",
                "🔥 Se vad som sålts i <#sålda-fynd>",
                "💎 Få fynden direkt i <#vip-telegram>",
                "💬 Säg hej i <#snack>!",
            ])),
            ("regler", "Läs innan du skriver", True, "\n".join([
                "📜 **Regler**",
                "",
                "1. Var schysst – inga påhopp, ingen spam.",
                "2. Inga kopior eller fejkade plagg – varken att sälja eller tipsa om.",
                "3. Betala alltid via Vinted eller plattformen du köper på – aldrig Swish till okända.",
                "4. Ingen reklam för andra grupper eller servrar utan lov.",
                "5. Handla på egen risk – kolla äktheten innan du köper.",
                "",
                "Bryter du mot reglerna kan du bli utkastad.",
            ])),
            ("vip-telegram", "Fynden direkt i mobilen", True, vip_text(link)),
        ]),
        ("🔥 FYND", [
            ("sålda-fynd", "Fynd som VIP-medlemmarna hann köpa", True, "\n".join([
                "🔥 **Sålda fynd**",
                "",
                "Här lägger boten upp fynd som redan har sålts – så att du ser hur snabbt de bästa plaggen går och "
                "vad det går att tjäna. Vill du få fynden *innan* de säljs: <#vip-telegram>.",
            ])),
        ]),
        ("💬 COMMUNITY", [
            ("snack", "Prata om allt som har med resell att göra", False, ""),
            ("legit-check", "Osäker på om något är äkta? Lägg upp bilder här", False, "\n".join([
                "🔍 **Legit check**",
                "",
                "Lägg upp länken eller bilder på plagget: etiketter, tvättlapp, sömmar, dragkedja och logga. "
                "Fler bilder ger säkrare svar.",
            ])),
            ("mina-vinster", "Visa vad du tjänat", False, "\n".join([
                "🏆 **Mina vinster**",
                "",
                "Visa dina bästa resells: bild + vad du köpte och sålde för, t.ex. *Köpt 450, såld 1 200*.",
            ])),
            ("köp-sälj-byt", "Sälj och byt med varandra", False, "\n".join([
                "🤝 **Köp, sälj & byt**",
                "",
                "Skriv vad du säljer eller letar efter, med bild, storlek och pris. Gör affärer på egen risk – "
                "använd gärna Vinted så att ni har köparskydd.",
            ])),
            ("tips", "Tips om att köpa och sälja", False, "\n".join([
                "💡 **Tips**",
                "",
                "Dela det du lärt dig: bra märken, prissättning, bilder, frakt och säsonger.",
            ])),
        ]),
    ]


def build(token: str, guild_id: str | None, link: str, api=vb.discord_api) -> list[str]:
    """Skapar det som saknas. Returnerar namnen på kanalerna som skapades."""
    guilds = api(token, "GET", "/users/@me/guilds") or []
    if guild_id is None:
        if len(guilds) != 1:
            raise SystemExit(f"Boten är med i {len(guilds)} servrar – sätt DISCORD_GUILD_ID till rätt server.")
        guild_id = str(guilds[0]["id"])
    existing = api(token, "GET", f"/guilds/{guild_id}/channels") or []
    categories = {c["name"]: str(c["id"]) for c in existing if c.get("type") == 4}
    channels = {c["name"]: str(c["id"]) for c in existing if c.get("type") == 0}
    created: list[str] = []
    texts: list[tuple[str, str]] = []
    for position, (category, rows) in enumerate(layout(link)):
        if category not in categories:
            categories[category] = str(api(token, "POST", f"/guilds/{guild_id}/channels",
                                           {"name": category, "type": 4, "position": position})["id"])
        for name, topic, read_only, text in rows:
            if name in channels:
                continue
            payload = {"name": name, "type": 0, "parent_id": categories[category], "topic": topic}
            if read_only:  # @everyone-rollen har samma id som servern
                payload["permission_overwrites"] = [{"id": guild_id, "type": 0, "deny": str(READ_ONLY)}]
            channels[name] = str(api(token, "POST", f"/guilds/{guild_id}/channels", payload)["id"])
            created.append(name)
            if text:
                texts.append((name, text))
    for name, text in texts:  # efter att alla kanaler finns, så att länkarna mellan dem fungerar
        body = text
        for other, cid in channels.items():
            body = body.replace(f"<#{other}>", f"<#{cid}>")
        msg = api(token, "POST", f"/channels/{channels[name]}/messages", {"content": body[:2000]})
        try:
            api(token, "PUT", f"/channels/{channels[name]}/pins/{msg['id']}")
        except vb.NotifyError:
            pass  # fästa är bara en bonus
    return created


def join_link(db: Path) -> str:
    """Ansökningslänken från /lank, om boten har skapat en."""
    if not db.exists():
        return ""
    store = vb.Store(db)
    try:
        return next((v for _, v in store.settings_starting_with("join:link:")), "")
    finally:
        store.db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(Path(__file__).with_name("vinted_bot.db")))
    args = parser.parse_args(argv)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("Lägg in din Discord-bots token som hemligheten DISCORD_BOT_TOKEN först.")
        return 1
    link = os.environ.get("JOIN_LINK", "").strip() or join_link(Path(args.db))
    try:
        created = build(token, os.environ.get("DISCORD_GUILD_ID") or None, link)
    except vb.NotifyError as e:
        hint = {401: "Fel token – kopiera en ny under Bot › Reset Token.",
                403: "Boten saknar rättigheter – bjud in den med rätten Administratör."}.get(e.status, "")
        print(f"Discord svarade {e.status}. {hint}")
        return 1
    print(f"Klart! Skapade {len(created)} kanaler: {', '.join(created) or 'inga nya'}.")
    if not link:
        print("Ingen ansökningslänk hittades – skriv /lank till boten i Telegram och kör igen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
