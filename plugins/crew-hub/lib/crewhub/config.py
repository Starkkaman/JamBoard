"""~/.claude/crew-hub/config.json (0600) plus secrets.json (0600). No `mqtt` block means file-only mode.

config.json never holds a password (other sessions read it):
{
  "machineId": "nas",            default: hostname
  "machineName": "NAS",          default: machineId
  "prefix": "crew",
  "transport": "mqtt",           "mqtt" (default) or "webhook" (docs/WEBHOOK.md, roaming machines)
  "mqtt": {"host": "...", "port": 1883, "username": "..."},
  "webhook": {"url": "https://hass.example.com"},   transport webhook only: HA's public URL (https only)
  "contextMax": {"<model substring>": tokens},
  "summaries": true,             default true; false = the hook never records Stop/SubagentStop messages
                                   (read by crew-hook itself, see hook.summaries_enabled)
  "commands": {                  signed commands from Home Assistant (docs/COMMANDS.md); off unless enabled AND
    "enabled": false,            secrets.json has commandSecret
    "folders": {"<key>": {"path": "/abs/path", "label": "Shown in HA"}},   start presets (key: [A-Za-z0-9_-]{1,40})
    "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
    "permissionModes": ["default", "acceptEdits", "auto", "plan"],
    "holdSeconds": 55            how long crew-hook waits for an answer to a permission prompt/question (0..55;
  }                                0 = publish only). Also read by crew-hook itself (hook.commands_hold).
}

secrets.json: {"mqttPassword": "...", "commandSecret": "..." (signed commands; transport webhook: also signs every
request), "webhookId": "..." (transport webhook only)}. crew-hub-pair (pair.py) writes both files.
Password order: secrets.json mqttPassword, then the legacy config.json mqtt.password. load(migrate=True), called by
crew-hubd under its pidfile lock, moves a legacy password into secrets.json and rewrites config.json without it.
Nothing here ever logs a secret value.
"""

import json
import os
import re
import socket
import stat
from urllib.parse import urlsplit

from . import paths


DEFAULT_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
DEFAULT_MODES = ["default", "acceptEdits", "auto", "plan"]
HOLD_DEFAULT = 55
HOLD_MAX = 55  # hooks.json gives PermissionRequest a 65 s timeout: the hold must end well before it
FOLDER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,79}$")
MODE_RE = re.compile(r"^[A-Za-z]{1,30}$")
WEBHOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
# Tests only: plain http is accepted for a loopback host when this is "1". Real hubs are always https.
TEST_HTTP_ENV = "CREWHUB_TEST_ALLOW_HTTP"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def parse_webhook_url(url):
    """{"scheme", "host", "port", "base"} for HA's public URL, or ValueError(reason). The reason never repeats the URL."""
    if not isinstance(url, str) or not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("not a URL")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise ValueError("not a URL")
    scheme = (p.scheme or "").lower()
    host = p.hostname
    if not host:
        raise ValueError("no host")
    if p.username is not None or p.password is not None:
        raise ValueError("credentials in the URL")
    if p.query or p.fragment:
        raise ValueError("query or fragment in the URL")
    if scheme == "https":
        default = 443
    elif scheme == "http" and os.environ.get(TEST_HTTP_ENV) == "1" and host in LOOPBACK:
        default = 80
    else:
        raise ValueError("must start with https://")
    return {"scheme": scheme, "host": host, "port": port or default, "base": p.path.rstrip("/")}


def hold_seconds(value):
    """holdSeconds clamped to 0..HOLD_MAX; anything odd means the default."""
    if isinstance(value, bool):
        return HOLD_DEFAULT
    try:
        v = float(value)
    except Exception:
        return HOLD_DEFAULT
    if v != v:  # NaN
        return HOLD_DEFAULT
    return max(0.0, min(float(HOLD_MAX), v))


def _str_list(value, default, pattern):
    if not isinstance(value, list):
        return list(default)
    out = []
    for v in value:
        if isinstance(v, str) and pattern.match(v) and v not in out:
            out.append(v)
    return out


class Commands(object):
    """The "commands" block. `active` needs enabled + a command secret; folders/models/modes are the allowlist."""

    def __init__(self, data, secret):
        data = data if isinstance(data, dict) else {}
        self.enabled = data.get("enabled") is True
        self.secret = secret
        self.folders = {}
        folders = data.get("folders")
        if isinstance(folders, dict):
            for key, v in folders.items():
                if not (isinstance(key, str) and FOLDER_KEY_RE.match(key) and isinstance(v, dict)):
                    continue
                path = v.get("path")
                if not (isinstance(path, str) and os.path.isabs(path)):
                    continue
                label = v.get("label") if isinstance(v.get("label"), str) and v.get("label").strip() else key
                self.folders[key] = {"path": os.path.normpath(path), "label": label.strip()[:60]}
        self.models = _str_list(data.get("models"), DEFAULT_MODELS, MODEL_RE)
        self.permission_modes = _str_list(data.get("permissionModes"), DEFAULT_MODES, MODE_RE)
        self.hold_seconds = hold_seconds(data.get("holdSeconds", HOLD_DEFAULT))

    @property
    def active(self):
        return bool(self.enabled and self.secret)

    def public(self):
        """machine.commands for the snapshot: keys and labels only, never paths. None when inactive."""
        if not self.active:
            return None
        return {
            "enabled": True,
            "folders": [{"key": k, "label": v["label"]} for k, v in sorted(self.folders.items())],
            "models": list(self.models),
            "permissionModes": list(self.permission_modes),
        }


def _topic_safe(value):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-").lower()
    return s[:64] or "machine"


class Config(object):
    def __init__(self, data=None, secrets=None):
        data = data if isinstance(data, dict) else {}
        secrets = secrets if isinstance(secrets, dict) else {}
        host = socket.gethostname().split(".")[0] or "machine"
        self.machine_id = _topic_safe(data.get("machineId") or host)
        self.machine_name = str(data.get("machineName") or data.get("machineId") or host)[:80]
        self.prefix = _topic_safe(data.get("prefix") or "crew")
        self.transport = "webhook" if data.get("transport") == "webhook" else "mqtt"
        self.mqtt = None
        self.webhook = None
        self.webhook_error = None  # why transport webhook is unusable (safe to log: never a URL or a secret)
        m = data.get("mqtt")
        if self.transport == "mqtt" and isinstance(m, dict) and m.get("host"):
            try:
                port = int(m.get("port") or 1883)
            except Exception:
                port = 1883
            password = secrets.get("mqttPassword")
            if not isinstance(password, str):
                password = m.get("password") if isinstance(m.get("password"), str) else None  # legacy
            self.mqtt = {
                "host": str(m["host"]),
                "port": port,
                "username": m.get("username") if isinstance(m.get("username"), str) else None,
                "password": password,
            }
        cs = secrets.get("commandSecret")
        self.command_secret = cs if isinstance(cs, str) and cs else None
        self.commands = Commands(data.get("commands"), self.command_secret)
        if self.transport == "webhook":
            self._webhook(data.get("webhook"), secrets.get("webhookId"))
        cm = data.get("contextMax")
        self.context_max = {}
        if isinstance(cm, dict):
            for k, v in cm.items():
                try:
                    self.context_max[str(k).lower()] = int(v)
                except Exception:
                    pass

    def _webhook(self, block, webhook_id):
        url = block.get("url") if isinstance(block, dict) else None
        if not url:
            self.webhook_error = "config.json has no webhook.url"
            return
        try:
            target = parse_webhook_url(url)
        except ValueError as e:
            self.webhook_error = "config.json webhook.url unusable (%s)" % e
            return
        if not (isinstance(webhook_id, str) and WEBHOOK_ID_RE.match(webhook_id)):
            self.webhook_error = "secrets.json has no usable webhookId"
            return
        if not self.command_secret:
            self.webhook_error = "secrets.json has no commandSecret"
            return
        target["id"] = webhook_id
        target["secret"] = self.command_secret
        self.webhook = target

    @property
    def linked(self):
        """True when snapshots go somewhere besides snapshot.json (MQTT or webhook)."""
        return bool(self.mqtt or self.webhook)

    def topic(self, leaf):
        return "%s/%s/%s" % (self.prefix, self.machine_id, leaf)

    def ctx_max(self, model):
        m = (model or "").lower()
        # Longest matching substring wins so "opus-5" can override "opus".
        for key in sorted(self.context_max, key=len, reverse=True):
            if key and key in m:
                return self.context_max[key]
        if "opus" in m or "fable" in m:
            return 1000000
        return 200000


def _write_private(path, data):
    """Atomic JSON write with mode 0600 (temp file in the same dir, then rename)."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_private(path, name, log):
    """Parsed JSON object from a 0600 file (tightened if needed), {} when missing; None when unreadable."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, 0o600)
            if log:
                log("%s was readable by others; tightened to 0600" % name)
    except Exception:
        pass
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception as e:
        if log:
            log("%s unreadable (%s)" % (name, type(e).__name__))  # never the content: it may hold a secret
        return None
    return data if isinstance(data, dict) else None


def _legacy_password(data):
    m = data.get("mqtt") if isinstance(data, dict) else None
    return isinstance(m, dict) and isinstance(m.get("password"), str)


def migrate_password(data, log=None):
    """Move config.json mqtt.password into a new secrets.json, then rewrite config.json without it.

    Only when secrets.json doesn't exist yet. Returns the secrets dict now in effect (or None)."""
    if not _legacy_password(data) or os.path.lexists(paths.secrets_path()):
        return None
    secrets = {"mqttPassword": data["mqtt"]["password"]}
    try:
        _write_private(paths.secrets_path(), secrets)
    except Exception as e:
        if log:
            log("could not write secrets.json (%s); MQTT password stays in config.json" % type(e).__name__)
        return None
    clean = dict(data)
    clean["mqtt"] = {k: v for k, v in data["mqtt"].items() if k != "password"}
    try:
        _write_private(paths.config_path(), clean)
    except Exception as e:
        if log:
            log("secrets.json written but config.json rewrite failed (%s); remove mqtt.password by hand" % type(e).__name__)
        return secrets
    if log:
        log("migrated MQTT password to secrets.json")
    return secrets


def load(log=None, migrate=False):
    data = _read_private(paths.config_path(), "config.json", log)
    if data is None:
        if log:
            log("config.json unusable; using defaults, file-only mode")
        data = {}
    secrets = _read_private(paths.secrets_path(), "secrets.json", log)
    if secrets is None:
        secrets = {}
    if migrate and _legacy_password(data):
        moved = migrate_password(data, log)
        if moved is not None:
            secrets = moved
        elif log and isinstance(secrets.get("mqttPassword"), str):
            log("config.json still has mqtt.password; secrets.json takes precedence, remove it from config.json")
    return Config(data, secrets)
