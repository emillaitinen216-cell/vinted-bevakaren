#!/usr/bin/env bash
# Vinted-bevakaren – installerar det som behövs, kopplar Telegram första
# gången och startar bevakningen. Fungerar på Chromebook (Linux), Linux och Mac.
#
#   bash starta.sh              # starta bevakningen
#   bash starta.sh --dry-run    # extra flaggor skickas vidare till vinted_bot.py
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .venv/.klar ]; then
  echo "Förbereder boten (bara första gången, tar någon minut) …"
  rm -rf .venv
  if ! python3 -m venv .venv >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      echo "Installerar Python (kan fråga efter lösenord) …"
      sudo apt-get update -qq
      sudo apt-get install -y -qq python3 python3-venv
      rm -rf .venv
      python3 -m venv .venv
    else
      echo "Hittar inte Python 3. Installera det från https://www.python.org/downloads/ och kör igen."
      exit 1
    fi
  fi
  .venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
  touch .venv/.klar
fi

if ! grep -qE '^TELEGRAM_CHAT_ID=-?[0-9]+' .env 2>/dev/null; then
  echo
  echo "Nu kopplar vi Telegram."
  .venv/bin/python vinted_bot.py --telegram
  echo
fi

echo "Startar bevakningen. Låt fönstret vara öppet – stoppa med Ctrl+C."
exec .venv/bin/python vinted_bot.py "$@"
