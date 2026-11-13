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
import time
from pathlib import Path

import vinted_bot as vb

VIEW = 1 << 10                     # se kanalen
SEND = 1 << 11                     # skicka meddelanden
HISTORY = 1 << 16                  # läsa äldre meddelanden
THREADS = (1 << 35) | (1 << 38)    # skapa trådar och skriva i dem
READ_ONLY = SEND | THREADS
VIP_ROLE = "💎 VIP"

# AutoMod: Discords inbyggda skydd mot spam och bedragare. (namn, regel)
AUTOMOD = [
    ("Spam", {"event_type": 1, "trigger_type": 3,
              "actions": [{"type": 1, "metadata": {"custom_message": "Meddelandet stoppades som spam."}}]}),
    ("Massnämningar", {"event_type": 1, "trigger_type": 5,
                       "trigger_metadata": {"mention_total_limit": 5, "mention_raid_protection_enabled": True},
                       "actions": [{"type": 1, "metadata": {"custom_message": "För många nämningar i ett meddelande."}}]}),
    ("Bedrägerilänkar", {"event_type": 1, "trigger_type": 1,
                         "trigger_metadata": {"keyword_filter": [
                             "*free nitro*", "*gratis nitro*", "*discord.gift*", "*discordgift*", "*nitro-gift*",
                             "*steamcommunity.com/gift*", "*steam-gift*", "*airdrop*", "*claim your prize*"]},
                         "actions": [{"type": 1, "metadata": {
                             "custom_message": "Meddelandet liknar ett känt bedrägeri och stoppades."}}]}),
    ("Grova ord", {"event_type": 1, "trigger_type": 4, "trigger_metadata": {"presets": [2, 3]},
                   "actions": [{"type": 1, "metadata": {"custom_message": "Håll god ton i servern."}}]}),
]


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
            ("prischeck", "Skriv ett plagg – boten svarar vad det kostar på Vinted", False, "\n".join([
                "💰 **Gratis prischeck**",
                "",
                "Skriv märke och modell här, t.ex. *Moncler Maya* eller *Stone Island tröja*, så svarar boten vad "
                "det kostar på Vinted just nu: median, vanligt prisintervall och vad du bör sätta för att sälja snabbt.",
                "",
                "Du har 3 gratis prischeckar per dag. Obegränsat i <#vip-telegram>.",
            ])),
            ("bjud-in", "Bjud in 5 – få VIP för halva priset", False, "\n".join([
                "📨 **Bjud in 5 vänner – få VIP för halva priset!**",
                "",
                "1. Högerklicka på servern (eller tryck på servernamnet) › **Bjud in folk** › kopiera din länk.",
                "2. Skicka länken till kompisar som gillar att köpa och sälja kläder.",
                "3. När 5 har gått med via *din* länk skickar boten en rabattkod till dig privat.",
                "",
                "Skriv vad som helst här för att se hur många du har bjudit in.",
            ])),
            ("legit-check", "Osäker på om något är äkta? Lägg upp bilder här", False, "\n".join([
                "🔍 **Legit check**",
                "",
                "Klistra in en Vinted-länk så svarar boten med en riskbedömning: säljarens omdömen, bilderna, "
                "varningsord och vad du ska be säljaren fota (3 gratis per dag, obegränsat i <#vip-telegram>).",
                "",
                "Har du köpt något? Lägg upp bilder på etiketter, tvättlapp, sömmar, dragkedja och logga, så hjälper "
                "andra till.",
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
        ("💎 VIP", [
            ("verifiera", "VIP i Telegram? Koppla ditt Discord-konto här", False, "\n".join([
                "💎 **Är du VIP i Telegram-gruppen?**",
                "",
                "1. Skriv **/discord** privat till Telegram-boten.",
                "2. Klistra in koden du får (t.ex. `DC-K7Q2MX`) här.",
                "3. Du får rollen **💎 VIP** och tillgång till <#vip-snack>.",
                "",
                "Koden tas bort direkt efter att du skickat den. Inte VIP än? Se <#vip-telegram>.",
            ])),
            ("vip-fynd", "Fynden live – samma som i Telegram-gruppen", "vip-ro", "\n".join([
                "🔥 **VIP-fynd**",
                "",
                "Här kommer fynden direkt när boten hittar dem – med pris, uppskattad vinst och kopierisk. "
                "Slå på aviseringar för kanalen (klockan uppe till höger) så missar du inget.",
            ])),
            ("vip-säljhjälp", "Klistra in en Vinted-länk – få en färdig annonstext", "vip", "\n".join([
                "📝 **Säljhjälp**",
                "",
                "Klistra in länken till plagget du köpt, så gör boten en färdig annonstext med titel, beskrivning "
                "och prisförslag. Ta egna bilder i dagsljus – kopiera inte säljarens.",
                "",
                "Prischeck och legitcheck är dessutom obegränsade för dig som är 💎 VIP.",
            ])),
            ("vip-snack", "Bara för VIP-medlemmar", "vip", "\n".join([
                "💎 **Välkommen till VIP-snacket!**",
                "",
                "Här pratar VIP-medlemmarna fynd, strategier och vad som säljer just nu.",
            ])),
        ]),
    ]


def build(token: str, guild_id: str | None, link: str, api=vb.discord_api, pause: float = 1.0) -> list[str]:
    """Skapar det som saknas. Returnerar namnen på kanalerna som skapades."""
    raw_api = api

    def api(*args, **kwargs):  # lugnt tempo – Discord begränsar hur snabbt kanaler får skapas
        if args[1] != "GET" and pause:
            time.sleep(pause)
        return raw_api(*args, **kwargs)

    guilds = api(token, "GET", "/users/@me/guilds") or []
    if guild_id is None:
        if len(guilds) != 1:
            raise SystemExit(f"Boten är med i {len(guilds)} servrar – sätt DISCORD_GUILD_ID till rätt server.")
        guild_id = str(guilds[0]["id"])
    all_roles = api(token, "GET", f"/guilds/{guild_id}/roles") or []
    roles = {r.get("name"): str(r["id"]) for r in all_roles}
    if VIP_ROLE not in roles:  # guldfärgad, och visas för sig i medlemslistan
        roles[VIP_ROLE] = str(api(token, "POST", f"/guilds/{guild_id}/roles",
                                  {"name": VIP_ROLE, "color": 0xF1C40F, "hoist": True, "mentionable": False})["id"])
        print("Skapade rollen 💎 VIP.")
    else:
        lift_vip_role(token, guild_id, roles[VIP_ROLE], all_roles, api)
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
            if read_only in ("vip", "vip-ro"):  # dold för alla utom VIP-rollen (vip-ro: bara läsa)
                vip = {"id": roles[VIP_ROLE], "type": 0, "allow": str(VIEW | HISTORY | (SEND if read_only == "vip" else 0))}
                if read_only == "vip-ro":
                    vip["deny"] = str(READ_ONLY)
                payload["permission_overwrites"] = [{"id": guild_id, "type": 0, "deny": str(VIEW)}, vip]
            elif read_only:  # @everyone-rollen har samma id som servern
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
    automod(token, guild_id, api)
    return created


def lift_vip_role(token: str, guild_id: str, role_id: str, all_roles: list[dict], api) -> bool:
    """Guldfärg, egen grupp i medlemslistan och så högt det går (direkt under botens roll) – annars syns
    färgen inte för den som också har en annan färgad roll högre upp."""
    vip = next((r for r in all_roles if str(r["id"]) == role_id), {})
    if vip.get("color") != 0xF1C40F or not vip.get("hoist"):
        api(token, "PATCH", f"/guilds/{guild_id}/roles/{role_id}", {"color": 0xF1C40F, "hoist": True})
    me = api(token, "GET", "/users/@me") or {}
    member = api(token, "GET", f"/guilds/{guild_id}/members/{me.get('id')}") or {}
    positions = {str(r["id"]): int(r.get("position") or 0) for r in all_roles}
    top = max((positions.get(str(r), 0) for r in member.get("roles") or []), default=0)
    target = top - 1
    if target < 1 or positions.get(role_id) == target:
        return False
    try:
        api(token, "PATCH", f"/guilds/{guild_id}/roles", [{"id": role_id, "position": target}])
        print("Flyttade upp rollen 💎 VIP så att guldfärgen syns.")
        return True
    except vb.NotifyError as e:
        print(f"Kunde inte flytta rollen 💎 VIP (Discord svarade {e.status}) – dra den högt upp under "
              "Serverinställningar › Roller.")
        return False


def automod(token: str, guild_id: str, api) -> list[str]:
    """AutoMod – Discords skydd mot spam och bedragare. Varje regel för sig, så att ett fel inte stoppar resten."""
    try:
        rules = api(token, "GET", f"/guilds/{guild_id}/auto-moderation/rules") or []
        existing = {r.get("name") for r in rules}
        types = {r.get("trigger_type") for r in rules}
    except vb.NotifyError as e:
        print(f"Kunde inte läsa AutoMod (Discord svarade {e.status}) – gör det under Serverinställningar › AutoMod.")
        return []
    made = []
    for name, rule in AUTOMOD:
        if name in existing or (rule["trigger_type"] in (3, 5) and rule["trigger_type"] in types):
            continue  # spam- och nämningsregler får bara finnas en av varje – Discord har ofta redan en
        payload = {"name": name, "enabled": True, **rule}
        for attempt in (payload, {**payload, "actions": [{"type": 1}]}):  # utan eget meddelande om det nekas
            try:
                api(token, "POST", f"/guilds/{guild_id}/auto-moderation/rules", attempt)
                print(f"Slog på AutoMod: {name}.")
                made.append(name)
                break
            except vb.NotifyError as e:
                reason = " ".join(e.detail.split())[:200]
        else:
            print(f"Kunde inte slå på AutoMod-regeln {name} – Discord svarade: {reason}")
    return made


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
                403: "Boten saknar rättigheter – bjud in den med rätten Administratör.",
                429: "Discord begränsar just nu – vänta en kvart och kör igen. Det som redan skapats ligger kvar."
                }.get(e.status, "")
        print(f"Discord svarade {e.status}. {hint}")
        if e.status == 429:  # vad Discord sa (innehåller inga hemligheter) – visar om det är en tillfällig spärr
            print("Discords svar:", " ".join(e.detail.split())[:200])
        return 1
    print(f"Klart! Skapade {len(created)} kanaler: {', '.join(created) or 'inga nya'}.")
    if not link:
        print("Ingen ansökningslänk hittades – skriv /lank till boten i Telegram och kör igen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
