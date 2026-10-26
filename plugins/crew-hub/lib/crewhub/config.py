"""~/.claude/crew-hub/config.json (0600). No `mqtt` block means file-only mode.

{
  "machineId": "nas",            default: hostname
  "machineName": "NAS",          default: machineId
  "prefix": "crew",
  "mqtt": {"host": "...", "port": 1883, "username": "...", "password": "..."},
  "contextMax": {"<model substring>": tokens}
}
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
    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
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
            self.mqtt = {
                "host": str(m["host"]),
                "port": port,
                "username": m.get("username") if isinstance(m.get("username"), str) else None,
                "password": m.get("password") if isinstance(m.get("password"), str) else None,
            }
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


def load(log=None):
    path = paths.config_path()
    data = {}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return Config({})
    try:
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, 0o600)  # it holds the MQTT password
            if log:
                log("config.json was readable by others; tightened to 0600")
    except Exception:
        pass
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception as e:
        if log:
            log("config.json unreadable (%s); using defaults, file-only mode" % type(e).__name__)
        data = {}
    return Config(data)
