"""crew-hub-pair: set this machine up for Crew Hub. Run by a person in their own terminal, never through Claude.

Webhook (roaming machines, docs/WEBHOOK.md): reads the pairing code from Home Assistant (hidden input), writes
config.json (transport webhook, webhook.url, machineId, machineName, commands) and secrets.json (webhookId,
commandSecret), sends one signed hello, then restarts crew-hubd.

--mqtt (LAN machines): asks for the broker host, port, username, password (hidden), machine id and command secret
(hidden), writes the same two files with transport mqtt, then restarts crew-hubd.

Never prints, logs or echoes the code or a secret. --stdin reads the same answers from stdin, one per line (tests).
"""

import argparse
import base64
import getpass
import json
import os
import re
import signal
import socket
import sys
import time

from . import VERSION, config, paths

CODE_PREFIX = "crewhub1:"
CODE_MAX = 4096
MACHINE_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
SECRET_MIN = 16
STOP_WAIT = 10.0
START_WAIT = 5.0


class PairError(Exception):
    """A message for the person; never contains the code or a secret."""


# ---- input -------------------------------------------------------------------------------------------------------
class Prompter(object):
    def __init__(self, use_stdin):
        self.use_stdin = use_stdin

    def _line(self):
        line = sys.stdin.readline()
        if not line:
            raise PairError("not enough input on stdin")
        return line.rstrip("\r\n")

    def ask(self, prompt, default=None):
        if self.use_stdin:
            value = self._line().strip()
        else:
            shown = "%s [%s]: " % (prompt, default) if default else "%s: " % prompt
            value = input(shown).strip()
        return value or (default or "")

    def secret(self, prompt):
        if self.use_stdin:
            return self._line().strip()
        return getpass.getpass("%s (hidden): " % prompt).strip()


# ---- pairing code ------------------------------------------------------------------------------------------------
def parse_code(code):
    """{"u", "w", "s", "m"} from a crewhub1: code, plus "target" (parsed URL). PairError on anything off."""
    code = (code or "").strip()
    if not code.startswith(CODE_PREFIX):
        raise PairError("that is not a Crew Hub pairing code (it starts with %s)" % CODE_PREFIX)
    if len(code) > CODE_MAX:
        raise PairError("the pairing code is too long")
    raw = code[len(CODE_PREFIX):]
    if not re.match(r"^[A-Za-z0-9_-]+={0,2}$", raw):
        raise PairError("the pairing code is damaged (copy it again)")
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
    except Exception:
        raise PairError("the pairing code is damaged (copy it again)")
    if not isinstance(data, dict):
        raise PairError("the pairing code is damaged (copy it again)")
    for key in ("u", "w", "s", "m"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise PairError("the pairing code is incomplete (copy it again)")
    try:
        target = config.parse_webhook_url(data["u"])
    except ValueError as e:
        raise PairError("the Home Assistant URL in the code is unusable: %s" % e)
    if not config.WEBHOOK_ID_RE.match(data["w"]):
        raise PairError("the webhook id in the code is unusable")
    if len(data["s"]) < SECRET_MIN:
        raise PairError("the secret in the code is too short")
    if not MACHINE_RE.match(data["m"]):
        raise PairError("the machine id in the code is unusable")
    return {"u": data["u"], "w": data["w"], "s": data["s"], "m": data["m"], "target": target}


# ---- files -------------------------------------------------------------------------------------------------------
def _ensure_dir():
    d = paths.ensure_hub_dir()
    try:
        if os.stat(d).st_mode & 0o077:
            os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _read(path, name):
    data = config._read_private(path, name, None)
    if data is None:
        raise PairError("%s exists but isn't readable JSON; fix or remove it, then run this again" % name)
    return data


def _commands_block(existing):
    block = dict(existing) if isinstance(existing, dict) else {}
    block["enabled"] = True
    if not isinstance(block.get("folders"), dict):
        block["folders"] = {}
    return block


def write_files(cfg_updates, secret_updates, drop_secrets=()):
    """Merge into config.json and secrets.json (both 0600, dir 0700), keeping every other key."""
    _ensure_dir()
    cfg = _read(paths.config_path(), "config.json")
    secrets = _read(paths.secrets_path(), "secrets.json")
    cfg.update(cfg_updates)
    cfg["commands"] = _commands_block(cfg.get("commands"))
    if isinstance(cfg.get("mqtt"), dict) and "password" in cfg["mqtt"]:
        cfg["mqtt"] = {k: v for k, v in cfg["mqtt"].items() if k != "password"}  # passwords live in secrets.json
    for k in drop_secrets:
        secrets.pop(k, None)
    secrets.update(secret_updates)
    config._write_private(paths.secrets_path(), secrets)
    config._write_private(paths.config_path(), cfg)
    return cfg


def default_name():
    return socket.gethostname().split(".")[0] or "machine"


def default_machine_id():
    return config._topic_safe(default_name())


# ---- daemon ------------------------------------------------------------------------------------------------------
def _daemon_pid():
    from . import hook

    if not hook.daemon_alive():
        return None
    try:
        with open(paths.pid_path(), "r") as f:
            return int(f.read().strip() or "0") or None
    except Exception:
        return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_free():
    """True once no crew-hubd holds the pidfile lock (it keeps an flock for its whole life)."""
    import fcntl

    try:
        fd = os.open(paths.pid_path(), os.O_RDONLY)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)  # closing drops the lock again
    return True


def restart_daemon(out):
    from . import hook

    pid = _daemon_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid = None
        end = time.time() + STOP_WAIT
        while pid and _alive(pid) and not _lock_free() and time.time() < end:
            time.sleep(0.1)
        if pid and _alive(pid) and not _lock_free():
            out("crew-hubd (pid %d) did not stop within %ds; stop it and run crew-hubd yourself" % (pid, int(STOP_WAIT)))
            return False
    hook.spawn_daemon()
    end = time.time() + START_WAIT
    while time.time() < end:
        if hook.daemon_alive():
            out("crew-hubd %s (log: %s)" % ("restarted" if pid else "started", paths.log_path()))
            return True
        time.sleep(0.1)
    out("crew-hubd did not start; run %s and check %s" % (
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "bin", "crew-hubd"),
        paths.log_path()))
    return False


# ---- flows -------------------------------------------------------------------------------------------------------
def pair_webhook(args, prompter, out):
    code = prompter.secret("Pairing code from Home Assistant")
    info = parse_code(code)
    existing = _read(paths.config_path(), "config.json")
    name = args.name or existing.get("machineName") or default_name()
    write_files(
        {"transport": "webhook", "webhook": {"url": info["u"]}, "machineId": info["m"], "machineName": str(name)[:80]},
        {"webhookId": info["w"], "commandSecret": info["s"]},
    )
    out("wrote %s and secrets.json (0600)" % paths.config_path())
    target = dict(info["target"], id=info["w"], secret=info["s"])
    host = target["host"]
    ok = True
    from . import webhook

    try:
        resp = webhook.post_once(target, info["m"], "hello", {"version": VERSION})
        if resp.get("ok") is not True:
            raise webhook.BadResponse("no ok")
        out("paired as %s with %s" % (info["m"], host))
    except Exception as e:
        ok = False
        out("hello to %s failed: %s" % (host, webhook.describe(e)))
        out("the settings are saved; crew-hubd keeps retrying. If this says HTTP 401 or unknown webhook, "
            "make a new pairing code in Home Assistant and run this again.")
    return ok


def pair_mqtt(args, prompter, out):
    existing = _read(paths.config_path(), "config.json")
    old = existing.get("mqtt") if isinstance(existing.get("mqtt"), dict) else {}
    host = prompter.ask("MQTT broker host", old.get("host"))
    if not host or any(c.isspace() for c in host):
        raise PairError("a broker host is needed")
    port_s = prompter.ask("MQTT port", str(old.get("port") or 1883))
    try:
        port = int(port_s)
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        raise PairError("the port must be a number from 1 to 65535")
    username = prompter.ask("MQTT username", old.get("username"))
    password = prompter.secret("MQTT password (empty = keep the saved one)")
    machine = prompter.ask("Machine id (as added in Crew Hub)", existing.get("machineId") or default_machine_id())
    if not MACHINE_RE.match(machine):
        raise PairError("the machine id may only use a-z, 0-9, _ and - (at most 64)")
    was_webhook = existing.get("transport") == "webhook"
    command_secret = prompter.secret("Command secret (same as in Crew Hub; empty = %s)" % (
        "commands off" if was_webhook else "keep the saved one, or commands off"))
    if command_secret and len(command_secret) < SECRET_MIN:
        raise PairError("the command secret must be at least %d characters" % SECRET_MIN)
    mqtt = {"host": host, "port": port}
    if username:
        mqtt["username"] = username
    updates = {"transport": "mqtt", "mqtt": mqtt, "machineId": machine}
    if args.name:
        updates["machineName"] = args.name[:80]
    secrets = {}
    if password:
        secrets["mqttPassword"] = password
    if command_secret:
        secrets["commandSecret"] = command_secret
    # the webhook's secret belongs to the webhook pairing: never reuse it for MQTT commands
    drop = ("webhookId", "commandSecret") if was_webhook else ("webhookId",)
    cfg = write_files(updates, secrets, drop_secrets=drop)
    have_secret = isinstance(_read(paths.secrets_path(), "secrets.json").get("commandSecret"), str)
    if not have_secret:
        cfg["commands"]["enabled"] = False
        config._write_private(paths.config_path(), cfg)
    out("wrote %s and secrets.json (0600); machine %s, broker %s:%d" % (paths.config_path(), machine, host, port))
    return True


FOLDER_HELP = """\
Commands are on. To let Home Assistant start sessions in a folder, add a preset to config.json "commands.folders",
for example: "folders": {"projects": {"path": "/Users/you/projects", "label": "Projects"}}
(the folder must be trusted in Claude Code: open Claude there once). Restart crew-hubd after editing."""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crew-hub-pair", description="Pair this machine with the Home Assistant Crew Hub.")
    ap.add_argument("--mqtt", action="store_true", help="LAN machine: use the MQTT broker instead of a pairing code")
    ap.add_argument("--name", help="display name for this machine (default: the hostname)")
    ap.add_argument("--stdin", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-daemon", action="store_true", help="don't start or restart crew-hubd")
    args = ap.parse_args(argv)

    def out(msg):
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    prompter = Prompter(args.stdin)
    try:
        ok = (pair_mqtt if args.mqtt else pair_webhook)(args, prompter, out)
    except PairError as e:
        sys.stderr.write("crew-hub-pair: %s\n" % e)
        return 2
    except (KeyboardInterrupt, EOFError):
        sys.stderr.write("\ncrew-hub-pair: cancelled\n")
        return 130
    cfg = _read(paths.config_path(), "config.json")
    if cfg.get("commands", {}).get("enabled") and not cfg["commands"].get("folders"):
        out(FOLDER_HELP)
    if not args.no_daemon:
        restart_daemon(out)
    return 0 if ok else 1
