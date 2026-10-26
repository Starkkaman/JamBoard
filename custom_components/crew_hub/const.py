"""Constants for the Crew Hub integration."""

DOMAIN = "crew_hub"
NAME = "Crew Hub"
VERSION = "0.1.1"  # keep equal to manifest.json "version"

CONF_PREFIX = "prefix"
CONF_MACHINES = "machines"
CONF_MACHINE_ID = "id"
CONF_MACHINE_NAME = "name"
CONF_MACHINE_SECRET = "secret"

DEFAULT_PREFIX = "crew"

# hass.data keys (the store outlives config entry reloads so websocket subscribers keep their feed)
DATA_STORE = f"{DOMAIN}_store"
DATA_FRONTEND = f"{DOMAIN}_frontend_registered"

CARD_FILENAME = "crew-hub-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"

WS_SUBSCRIBE = f"{DOMAIN}/subscribe"
WS_GET = f"{DOMAIN}/get"
