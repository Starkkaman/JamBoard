"""~/.claude/crew-hub/config.json (0600) plus secrets.json (0600). No `mqtt` block means file-only mode.

config.json never holds a password (other sessions read it):
{
  "machineId": "nas",            default: hostname
  "machineName": "NAS",          default: machineId
  "prefix": "crew",
  "mqtt": {"host": "...", "port": 1883, "username": "..."},
  "contextMax": {"<model substring>": tokens},
  "summaries": true              default true; false = the hook never records Stop/SubagentStop messages
}                                  (read by crew-hook itself, see hook.summaries_enabled)

secrets.json: {"mqttPassword": "...", "commandSecret": "..." (optional, for signed commands later)}
Password order: secrets.json mqttPassword, then the legacy config.json mqtt.password. load(migrate=True), called by
crew-hubd under its pidfile lock, moves a legacy password into secrets.json and rewrites config.json without it.
Nothing here ever logs a secret value.
"""

import json
import os
import re
import socket
import stat

from . import paths


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
        self.mqtt = None
        m = data.get("mqtt")
        if isinstance(m, dict) and m.get("host"):
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
        cm = data.get("contextMax")
        self.context_max = {}
        if isinstance(cm, dict):
            for k, v in cm.items():
                try:
                    self.context_max[str(k).lower()] = int(v)
                except Exception:
                    pass

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
