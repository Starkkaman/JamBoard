#!/bin/sh
# Copy the card source into the integration's www folder (served at /crew_hub/crew-hub-card.js).
# Run after every edit of card/crew-hub-card.js; tests/test_card_sync.py fails if the copies differ.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
cp "$root/card/crew-hub-card.js" "$root/custom_components/crew_hub/www/crew-hub-card.js"
echo "synced card/crew-hub-card.js -> custom_components/crew_hub/www/crew-hub-card.js"
