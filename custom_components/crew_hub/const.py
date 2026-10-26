"""Constants for the Crew Hub integration."""

DOMAIN = "crew_hub"
NAME = "Crew Hub"
VERSION = "0.3.0"  # keep equal to manifest.json "version"

CONF_PREFIX = "prefix"
CONF_MACHINES = "machines"
CONF_MACHINE_ID = "id"
CONF_MACHINE_NAME = "name"
CONF_MACHINE_SECRET = "secret"
CONF_MACHINE_TRANSPORT = "transport"  # absent means mqtt (machines added before 0.3.0)
CONF_MACHINE_PUBLIC_URL = "public_url"  # webhook machines: https://host[:port], put into the pairing code
CONF_MACHINE_WEBHOOK_ID = "webhook_id"  # webhook machines: secrets.token_hex(32); a secret like the command secret

TRANSPORT_MQTT = "mqtt"
TRANSPORT_WEBHOOK = "webhook"
TRANSPORTS = (TRANSPORT_MQTT, TRANSPORT_WEBHOOK)

DEFAULT_PREFIX = "crew"

# hass.data keys (the store outlives config entry reloads so websocket subscribers keep their feed)
DATA_STORE = f"{DOMAIN}_store"
DATA_FRONTEND = f"{DOMAIN}_frontend_registered"
DATA_WEBHOOK = f"{DOMAIN}_webhook_hub"  # webhook_transport.WebhookHub (queues, held polls, online timers)

CARD_FILENAME = "crew-hub-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"

WS_SUBSCRIBE = f"{DOMAIN}/subscribe"
WS_GET = f"{DOMAIN}/get"
WS_COMMAND = f"{DOMAIN}/command"

# hass.data key: pending command id -> (machine, asyncio.Future) waiting for <prefix>/<machine>/ack
DATA_PENDING = f"{DOMAIN}_pending"
