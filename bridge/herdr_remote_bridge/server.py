#!/usr/bin/env python3
"""Herd bridge: lets the Herd iPhone app see and drive herdr's agents.

Listens only on your Tailscale address and drives herdr through its own tools:
`hc` (herdr-core: list, read, send text, press keys) and `herdr-app` (spaces and tabs).
Works on macOS/Linux, and in WSL driving the Windows app (point the paths at the .exe files).

Settings come from the user config directory or HERD_CONFIG, and
environment variables override them. Nothing machine-specific lives in this file.
"""
import platform
import hashlib
import hmac
import socket
from pathlib import Path
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .herd_push import (PushConfig, TokenStore, APNsClient, RelayConfig, RelayClient,
                        StatusWatcher, assistant_summary, handle_register, handle_activity, handle_test_push)
from .herd_extras import SpaceStore, handle_chat, handle_upload, handle_dirs, handle_space
from . import sessions, herd_extras


def config_path():
    return Path(os.environ.get("HERD_CONFIG", str(Path.home() / ".config" / "herdr-remote-bridge" / "config.json"))).expanduser()


HERE = str(config_path().parent)


def load_config():
    cfg = {}
    path = config_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    env = {"host": "HERD_HOST", "port": "HERD_PORT", "hc": "HERD_HC", "herdr_app": "HERD_HERDR_APP", "home": "HERD_HOME", "token": "HERD_TOKEN"}
    for key, var in env.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    return cfg


def parse_interface_ipv4(output, method):
    """Return the first CGNAT IPv4 from CLI/interface output, in output order."""
    patterns = {
        'ifconfig': r'\binet\s+(\d+\.\d+\.\d+\.\d+)',
        'ip': r'\binet\s+(\d+\.\d+\.\d+\.\d+)',
        'ipconfig': r'IPv4 Address[^:\r\n]*:\s*(\d+\.\d+\.\d+\.\d+)',
        'cli': r'(?<![\d.])(\d+\.\d+\.\d+\.\d+)(?![\d.])',
    }
    for value in re.findall(patterns[method], output):
        try:
            if ipaddress.IPv4Address(value) in ipaddress.ip_network('100.64.0.0/10'):
                return value
        except ValueError:
            continue
    return None


def detect_tailscale():
    """Return (IPv4, detection method); failed commands fall through."""
    system = platform.system()
    known = {'Darwin': ['/Applications/Tailscale.app/Contents/MacOS/Tailscale'],
             'Windows': [str(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Tailscale' / 'tailscale.exe')],
             'Linux': ['/usr/bin/tailscale', '/usr/sbin/tailscale']}.get(system, [])
    candidates = [shutil.which('tailscale'), *known]
    seen = set()
    def run(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=10)
            return result.stdout if result.returncode == 0 else ''
        except (OSError, subprocess.TimeoutExpired):
            return ''
    for executable in candidates:
        if not executable or executable in seen:
            continue
        seen.add(executable)
        address = parse_interface_ipv4(run([executable, 'ip', '-4']), 'cli')
        if address:
            return address, executable + ' ip -4'
    method, args = ('ipconfig', ['ipconfig']) if system == 'Windows' else (
        ('ip', ['ip', '-4', '-o', 'addr']) if system == 'Linux' else ('ifconfig', ['/sbin/ifconfig']))
    address = parse_interface_ipv4(run(args), method)
    return address, method if address else None


def tailscale_ip():
    """This machine's Tailscale IPv4 address, or None."""
    return detect_tailscale()[0]


def _config_mtime():
    try:
        return config_path().stat().st_mtime_ns
    except OSError:
        return None


CONFIG = load_config()
_CONFIG_MTIME = _config_mtime()


def current_token():
    """The API token. Re-read from config.json when that file changes, so a new
    `pair` takes effect without restarting the bridge. HERD_TOKEN still wins."""
    global _CONFIG_MTIME
    if os.environ.get("HERD_TOKEN"):
        return os.environ["HERD_TOKEN"]
    mtime = _config_mtime()
    if mtime is not None and mtime != _CONFIG_MTIME:
        _CONFIG_MTIME = mtime
        try:
            CONFIG["token"] = json.loads(config_path().read_text(encoding="utf-8")).get("token")
        except (OSError, ValueError):
            pass  # half-written file: keep the last good token, retry on the next change
    return CONFIG.get("token")


def token_fingerprint(token):
    return hashlib.sha256(str(token).encode()).hexdigest()[:16]


HC = CONFIG.get("hc") or shutil.which("hc") or "hc"
HERDR_APP = CONFIG.get("herdr_app") or shutil.which("herdr-app") or "herdr-app"
HOME = CONFIG.get("home") or None  # folder new spaces start in when none is given
HOST = CONFIG.get("host") or tailscale_ip()
PORT = int(CONFIG.get("port") or 8787)
# What herdr's own + menu runs for each kind of tab (no argv = the machine's shell).
AGENT_ARGV = {"claude-code": ["claude"], "codex": ["codex"], "sh": None}
NEW_LOCK = threading.Lock()

TAILNET = ipaddress.ip_network("100.64.0.0/10")
ADDRESS = re.compile(r"^t[0-9a-z]{1,8}$")  # hc addresses: t7, t1b, ...
KEYS = {"enter", "esc", "tab", "up", "down", "left", "right", "ctrl+c", "y", "n", "1", "2", "3", "space", "backspace"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)")


def hc(*args, timeout=20):
    r = subprocess.run([HC, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def hc_rpc(method, params, timeout=60):
    """Call the hc API directly. Unlike the hc CLI, this sends no `caller`, so typed text
    isn't wrapped as <hc from="tN"> (Windows programs started from WSL inherit
    HC_TERMINAL_ID from whichever terminal launched WSL, which made the CLI tag the phone's
    messages as coming from that agent)."""
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {"generation": 1}}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})]
    p = subprocess.Popen([HC, "connect"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace",
                         env={k: v for k, v in os.environ.items() if k != "HC_TERMINAL_ID"})
    try:
        p.stdin.write(lines[0] + "\n"); p.stdin.flush()
        p.stdout.readline()
        p.stdin.write(lines[1] + "\n"); p.stdin.flush()
        reply = json.loads(p.stdout.readline() or "{}")
    finally:
        try:
            p.stdin.close()
            p.wait(timeout=5)
        except Exception:
            p.kill()
    if "error" in reply or "result" not in reply:
        err = reply.get("error", {})
        return 1, "", str(err.get("message") or err or "hc failed")
    return 0, json.dumps(reply["result"]), ""


def app(*args, timeout=20):
    r = subprocess.run([HERDR_APP, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def app_spaces():
    """This machine's herdr spaces, as the app shows them."""
    code, out, err = app("ls")
    if code:
        raise RuntimeError(err.strip() or out.strip() or "herdr-app ls failed")
    spaces = [s for s in json.loads(out).get("spaces", []) if not s.get("machine")]
    # The name /api/agents shows in "where" ("Project / 1") can differ from the
    # app's own name for an unnamed space ("~"); keep both so either one matches.
    where = {x["address"]: str(x.get("where", "")) for x in live_terminals()}
    for s in spaces:
        s["aliases"] = {s.get("name", "")}
        for tab in s.get("tabs", []):
            for term in tab.get("terminals", []):
                w = where.get(term.get("terminal"), "")
                if " / " in w:
                    s["aliases"].add(w.rsplit(" / ", 1)[0])
        s["label"] = next((a for a in s["aliases"] if a != s.get("name")), s.get("name", ""))
    return spaces


def dir_exists(path):
    """True when the folder exists. Windows paths (C:\\x) are checked through wslpath under WSL."""
    if WINDOWS_PATH.match(path) and (windows_host() or shutil.which("wslpath")):
        try:
            return os.path.isdir(wslpath(path, "u"))
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return False
    return os.path.isabs(path) and os.path.isdir(path)


WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_HOME = []


def windows_host():
    """True when this bridge runs in WSL but drives herdr on Windows (hc/herdr-app are .exe)."""
    return sessions.is_wsl() and any(str(p).lower().endswith(".exe") for p in (HC, HERDR_APP))


def wslpath(path, to):
    """Translate between Windows and WSL paths: to='u' gives /mnt/c/..., to='w' gives C:\\..."""
    r = subprocess.run(["wslpath", "-" + to, str(path)], capture_output=True, text=True, timeout=10)
    if r.returncode or not r.stdout.strip():
        raise ValueError("path translation failed")
    return r.stdout.strip()


def windows_home():
    """The Windows user's home (C:\\Users\\name): config `home` if it's a Windows path, else %USERPROFILE%."""
    if HOME and WINDOWS_PATH.match(HOME):
        return HOME
    if not _WINDOWS_HOME:
        out = subprocess.run(["cmd.exe", "/d", "/c", "echo %USERPROFILE%"], capture_output=True, text=True,
                             timeout=10, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None).stdout.strip()
        if not WINDOWS_PATH.match(out):
            raise ValueError("Windows home not found; set home in config.json")
        _WINDOWS_HOME.append(out)
    return _WINDOWS_HOME[0]


def agent_home():
    """Home folder whose .claude/.codex hold the agents' transcripts (the Windows one under WSL)."""
    return Path(wslpath(windows_home(), "u")) if windows_host() else Path.home()


def windows_dirs(query):
    """/api/dirs over the Windows home drive, in Windows paths, for a bridge running in WSL."""
    try:
        home = wslpath(windows_home(), "u")
        value = (query.get("path") or "").strip()
        linux = value if value.startswith("/") else wslpath(value or windows_home(), "u")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 400, {"error": "folder unavailable or outside browse root"}
    parts = Path(home).parts  # ('/', 'mnt', 'c', 'Users', ...)
    root = str(Path(*parts[:3])) if len(parts) >= 3 and parts[1] == "mnt" else "/"
    status, result = handle_dirs({"path": linux}, root=root)
    if status == 200:
        result["path"] = wslpath(result["path"], "w")
        if result.get("parent"):
            result["parent"] = wslpath(result["parent"], "w")
    return status, result


def terminal_info(t):
    code, out, err = hc("get", t, "--json")
    if code:
        raise RuntimeError(err.strip() or "hc get failed")
    return json.loads(out)


def chat_source(t, term):
    """Where terminal t's transcript lives: {'session_id'|'session_file', 'cwd', 'home'} or None.
    Claude: exact, via ~/.claude/sessions/<pid>.json under the terminal's process tree.
    Codex: the rollout that began in the terminal's folder after its codex process started."""
    agent = term.get("agent")
    if agent not in ("claude-code", "codex"):
        return None
    info = terminal_info(t)
    roots = [info.get("pid"), (info.get("foreground") or {}).get("pid")]
    home = agent_home()
    procs = sessions.processes(windows_host())
    if agent == "claude-code":
        found = sessions.claude_session(roots, home, procs)
        if found:
            return {"session_id": found["sessionId"], "cwd": found.get("cwd") or term.get("cwd"), "home": home}
        return None
    local = (lambda p: wslpath(p, "u") if WINDOWS_PATH.match(str(p)) else p) if windows_host() else None
    path = sessions.codex_rollout(roots, home, term.get("cwd"), procs, local)
    return {"session_file": str(path), "cwd": term.get("cwd"), "home": home} if path else None


def transcript_source(term, terminals):
    """Share /api/chat's explicit, process-mapped, and sole-cwd lookup order.

    The boolean indicates a process mapping whose missing file means empty chat.
    """
    if not term.get('session_file') and not term.get('session_id'):
        try:
            source = chat_source(term['address'], term)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            source = None
        if source:
            return source, True
        if not term.get('cwd') or sum(x.get('cwd') == term.get('cwd') for x in terminals) > 1:
            return None, False
    return {key: term.get(key) for key in ('cwd', 'session_file', 'session_id')} | {'home': agent_home()}, False


def terminal_summary(term):
    """Read the terminal's last assistant message when its transcript is available."""
    if term.get('agent') not in ('claude-code', 'codex'):
        return None
    try:
        source, _ = transcript_source(term, live_terminals())
        if source is None:
            return None
        path = herd_extras.find_transcript(term.get('agent'), **source)
        return assistant_summary(herd_extras.parse_transcript(path)) if path else None
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        return None


def agents_snapshot():
    code, out, err = hc("ls", "--json")
    if code:
        raise RuntimeError(err.strip() or "hc ls failed")
    return json.loads(out)


def live_terminals():
    return agents_snapshot().get("terminals", [])


class Runtime:
    def __init__(self, directory=None, get_terminals=None):
        self.directory = Path(directory or config_path().parent)
        self.machine = socket.gethostname()
        self.tokens = TokenStore(self.directory / 'herd-tokens.json')
        self.spaces = SpaceStore(self.directory / 'herd-spaces.json')
        self.client = None
        if (self.directory / 'herd-relay.json').exists():
            self.client = RelayClient(RelayConfig.load(self.directory), machine=self.machine)
        elif (self.directory / 'herd-push.json').exists():
            self.client = APNsClient(PushConfig.load(self.directory), self.tokens)
        self.watcher = (StatusWatcher(get_terminals or live_terminals, self.client, self.tokens, self.machine, summarize=terminal_summary)
                        if self.client else None)
        self._paired = None
        self._relay_mtime = self._relay_stamp()
        self._runtime_lock = threading.RLock()
        self._started = False
        self._get_terminals = get_terminals or live_terminals

    def _relay_stamp(self):
        try:
            stat = (self.directory / 'herd-relay.json').stat()
            return (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except FileNotFoundError:
            return None

    def reload_relay(self):
        with self._runtime_lock:
            stamp = self._relay_stamp()
            if stamp == self._relay_mtime:
                return
            try:
                config = RelayConfig.load(self.directory) if stamp else None
            except (OSError, ValueError, TypeError):
                return  # A partial write must not replace a working client.
            client = (RelayClient(config, machine=self.machine) if config else
                      APNsClient(PushConfig.load(self.directory), self.tokens)
                      if (self.directory / 'herd-push.json').exists() else None)
            self._close_push()
            self.client = client
            self.watcher = (StatusWatcher(self._get_terminals, client, self.tokens, self.machine, summarize=terminal_summary)
                            if client else None)
            self._relay_mtime = stamp
            if self._started and self.watcher:
                self.watcher.start()

    def note_paired(self, token, client_ip):
        """Record that a phone used this token, so `pair` can say it connected.
        Stores a fingerprint of the token, never the token itself."""
        path = self.directory / 'herd-paired.json'
        fingerprint = token_fingerprint(token)
        if self._paired == fingerprint and path.exists():
            return
        try:
            path.write_text(json.dumps({'token': fingerprint, 'from': client_ip, 'at': int(time.time())}),
                            encoding='utf-8')
            self._paired = fingerprint
        except OSError:
            pass

    def start(self):
        self._started = True
        self.reload_relay()
        if self.watcher and self.watcher.ident is None:
            self.watcher.start()

    def close(self):
        self._started = False
        self._close_push()

    def _close_push(self):
        if self.watcher:
            self.watcher.stop()
            if self.watcher.ident is not None:
                self.watcher.join()
            else:
                self.watcher.executor.shutdown(wait=True)
        if self.client:
            self.client.close()


class Handler(BaseHTTPRequestHandler):
    def _unsupported(self):
        if not self._allowed():
            return self._send(403, 'forbidden', 'text/plain')
        if self._authorized():
            self._json(404, {'error': 'not found'})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _unsupported

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler otherwise emits HTML for unimplemented HTTP methods.
        if code == 501 and urlparse(self.path).path.startswith('/api/'):
            return self._unsupported()
        return super().send_error(code, message, explain)

    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return ip in TAILNET or ip.is_loopback

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    @property
    def runtime(self):
        return self.server.runtime

    def _authorized(self):
        self.runtime.reload_relay()
        token = current_token()
        if urlparse(self.path).path.startswith('/api/') and token:
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + str(token)).encode()):
                self._json(401, {'error': 'unauthorized'})
                return False
            runtime = getattr(self.server, 'runtime', None)
            if runtime is not None and hasattr(runtime, 'note_paired'):
                runtime.note_paired(token, self.client_address[0])
        return True

    def do_GET(self):
        try:
            self._get()
        except OSError:
            self._json(500, {'error': 'storage unavailable'})
        except Exception:
            self._json(502, {'error': 'bridge operation failed'})

    def _get(self):
        if not self._allowed():
            return self._send(403, "forbidden", "text/plain")
        if not self._authorized():
            return
        url = urlparse(self.path)
        if url.path == "/":
            return self._json(200, {"app": "herd-phone", "ok": True})
        if url.path == '/api/pairing':
            client = self.runtime.client
            if not getattr(client, 'is_relay', False):
                return self._json(404, {'error': 'no relay'})
            if not current_token():
                return self._json(401, {'error': 'unauthorized'})
            config = client.config
            return self._json(200, {'relay': config.relay_url, 'bridge': config.bridge_id,
                                    'pair': config.pair_code})
        if url.path == "/api/agents":
            try:
                return self._json(200, agents_snapshot())
            except RuntimeError as exc:
                return self._json(502, {'error': str(exc)})
        query = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        if url.path == '/api/dirs':
            return self._json(*(windows_dirs(query) if windows_host() else handle_dirs(query)))
        if url.path == '/api/chat':
            t = query.get('t', '')
            if not ADDRESS.fullmatch(t):
                return self._json(400, {'error': 'bad terminal'})
            terminals = live_terminals()
            term = next((x for x in terminals if x['address'] == t), None)
            if not term:
                return self._json(404, {'error': 'terminal not found'})
            source, mapped = transcript_source(term, terminals)
            if source is None:
                return self._json(400, {'error': 'terminal session mapping required'})
            status, result = handle_chat(query, agent=term.get('agent'), **source)
            if mapped and status == 404:
                status, result = 200, {'messages': []}
            return self._json(status, result)
        if url.path == "/api/read":
            t = parse_qs(url.query).get("t", [""])[0]
            if not ADDRESS.fullmatch(t):
                return self._json(400, {"error": "bad terminal"})
            if not any(x["address"] == t for x in live_terminals()):
                return self._json(404, {"error": "terminal not found"})
            code, out, err = hc("read", t, "--lines", "300")
            if code:
                return self._json(502, {"error": err.strip() or "hc read failed"})
            return self._json(200, {"text": ANSI.sub("", out)})
        if url.path == "/api/spaces":
            try:
                spaces = app_spaces()
            except Exception as e:
                return self._json(502, {"error": str(e)})
            records = self.runtime.spaces.spaces(
                [{"name": s["label"], "cwd": s.get("folder") or ""} for s in spaces])
            for record in records:
                record['cwd'] = record.get('cwd') or HOME or ''
            return self._json(200, {"spaces": records})
        self._json(404, {"error": "not found"})

    def _new(self, body):
        space = str(body.get("space", "")).strip()
        agent = str(body.get("agent", ""))
        cwd = body.get("cwd")
        if not space or len(space) > 64:
            return self._json(400, {"error": "space required"})
        if agent not in AGENT_ARGV:
            return self._json(400, {"error": "agent must be claude-code, codex or sh"})
        if cwd is not None and not isinstance(cwd, str):
            return self._json(400, {"error": "bad cwd"})
        cwd = (cwd or "").strip()
        if cwd and not dir_exists(cwd):
            return self._json(400, {"error": "folder not found"})
        if cwd and cwd.startswith("/") and windows_host():
            cwd = wslpath(cwd, "w")  # herdr on Windows needs C:\... paths
        with NEW_LOCK:
            try:
                spaces = app_spaces()
                found = next((s for s in spaces if space in s["aliases"]), None) or \
                    next((s for s in spaces if space.lower() in {a.lower() for a in s["aliases"]}), None)
                folder = cwd or self.runtime.spaces.get(space) or (found and found.get("folder")) or HOME
                if folder and not dir_exists(folder):
                    return self._json(400, {"error": "folder not found"})
                before = {x["address"] for x in live_terminals()}
                args = {"folder": folder} if folder else {}
                if AGENT_ARGV[agent]:
                    args["argv"] = AGENT_ARGV[agent]
                if found and body.get("same_space") is True:
                    # Only when asked: another tab inside that existing space.
                    args["space"] = found["id"]
                    name = found["label"]
                    code, out, err = app("do", "tab.new", json.dumps(args))
                else:
                    # Default: every new agent gets its own space. A taken name gets a number
                    # ("My Project 2") so spaces stay apart; it still starts in that space's folder.
                    taken = {a.lower() for s in spaces for a in s["aliases"]}
                    name, n = space, 2
                    while name.lower() in taken:
                        name, n = f"{space} {n}", n + 1
                    args["name"] = name
                    code, out, err = app("do", "space.new", json.dumps(args))
                if code:
                    return self._json(502, {"error": (err or out).strip() or "herdr-app failed"})
                try:
                    address = json.loads(out).get("terminal")
                except (ValueError, AttributeError):
                    address = None
                deadline = time.time() + 15
                while time.time() < deadline:
                    terms = live_terminals()
                    if address:
                        mine = [x for x in terms if x["address"] == address]
                    else:
                        fresh = [x for x in terms if x["address"] not in before]
                        mine = [x for x in fresh if str(x.get("where", "")).startswith(name + " / ")] or fresh
                    if mine and mine[0].get("where"):
                        return self._json(200, {"address": mine[0]["address"], "where": mine[0]["where"]})
                    time.sleep(0.3)
                if address:
                    return self._json(200, {"address": address, "where": ""})
            except Exception as e:
                return self._json(502, {"error": str(e)})
        return self._json(504, {"error": "started, but the new terminal didn't show up"})

    def _close(self, body, project=False):
        value = body.get('space' if project else 't', '')
        if not isinstance(value, str) or not value.strip() or (not project and not ADDRESS.fullmatch(value)):
            return self._json(400, {'error': 'space required' if project else 'bad terminal'})
        with NEW_LOCK:
            try:
                spaces = app_spaces()
                if project:
                    found = next((s for s in spaces if value.strip().casefold() in
                                  {a.casefold() for a in s['aliases']}), None)
                    if not found:
                        return self._json(404, {'error': 'space not found'})
                    command, args = 'space.close', {'id': found['id'], 'confirmed': True}
                    count = sum(len(tab.get('terminals', [])) for tab in found.get('tabs', []))
                else:
                    match = next(((tab, term) for space in spaces for tab in space.get('tabs', [])
                                  for term in tab.get('terminals', []) if term.get('terminal') == value), None)
                    if match:
                        tab, term = match
                        if len(tab['terminals']) == 1:
                            command, args = 'tab.close', {'id': tab['id'], 'confirmed': True}
                        else:
                            command, args = 'pane.close', {'pane': term['pane']}
                    elif any(t['address'] == value for t in live_terminals()):
                        command, args = 'terminal.kill', {'terminal': value}
                    else:
                        return self._json(404, {'error': 'terminal not found'})
                code, out, err = app('do', command, json.dumps(args))
                if code:
                    return self._json(502, {'error': (err or out).strip() or 'herdr-app failed'})
                if project:
                    self.runtime.spaces.remove(found['aliases'])
                    return self._json(200, {'ok': True, 'closed_terminals': count})
                if command == 'terminal.kill':
                    try:
                        hc('rm', value)
                    except Exception:
                        pass
                return self._json(200, {'ok': True, 'closed': command.split('.')[0]})
            except Exception as exc:
                return self._json(502, {'error': str(exc)})

    def do_POST(self):
        try:
            self._post()
        except OSError:
            self._json(500, {'error': 'storage unavailable'})
        except Exception:
            self._json(502, {'error': 'bridge operation failed'})

    def _post(self):
        if not self._allowed():
            return self._send(403, "forbidden", "text/plain")
        if not self._authorized():
            return
        url = urlparse(self.path)
        if url.path not in ("/api/send", "/api/keys", "/api/new", "/api/register", "/api/activity",
                            "/api/test-push", "/api/upload", "/api/space", "/api/close", "/api/space/close"):
            return self._json(404, {"error": "not found"})
        try:
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length') is None:
                return self._json(400, {'error': 'Content-Length required; transfer encoding unsupported'})
            length = int(self.headers['Content-Length'])
            if length < 0:
                raise ValueError('negative length')
            if length > (21 * 1024 * 1024 if url.path == '/api/upload' else 20000):
                self.close_connection = True
                return self._json(413, {'error': 'request too large'})
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "bad json"})
        if url.path in ('/api/close', '/api/space/close'):
            return self._close(body, project=url.path == '/api/space/close')
        if url.path == '/api/register':
            return self._json(*handle_register(body, self.runtime.tokens))
        if url.path == '/api/activity':
            return self._json(*handle_activity(body, self.runtime.tokens))
        if url.path == '/api/test-push':
            if not self.runtime.client:
                return self._json(503, {'error': 'push is not configured'})
            return self._json(*handle_test_push(body, self.runtime.client, self.runtime.tokens))
        if url.path == '/api/upload':
            t = body.get('t', '')
            if not isinstance(t, str) or not ADDRESS.fullmatch(t):
                return self._json(400, {'error': 'bad terminal'})
            if not any(x['address'] == t for x in live_terminals()):
                return self._json(404, {'error': 'terminal not found'})
            return self._json(*handle_upload(body))
        if url.path == '/api/space':
            with NEW_LOCK:
                status, result = handle_space(body, self.runtime.spaces)
                if status != 200:
                    return self._json(status, result)
                name = body['space'].strip()
                if not any(name.casefold() in {a.casefold() for a in s['aliases']} for s in app_spaces()):
                    code, out, err = app('do', 'space.new', json.dumps({'name': name, 'folder': body['cwd']}))
                    if code:
                        return self._json(502, {'error': 'herdr-app failed; project default saved'})
            return self._json(status, result)
        if url.path == "/api/new":
            return self._new(body)
        t = str(body.get("t", ""))
        if not ADDRESS.fullmatch(t):
            return self._json(400, {"error": "bad terminal"})
        if url.path == "/api/send":
            text = str(body.get("text", ""))
            if not text.strip():
                return self._json(400, {"error": "empty"})
            code, out, err = hc_rpc("terminal.send", {"terminal": t, "text": text})
        elif url.path == "/api/keys":
            key = str(body.get("key", ""))
            if key not in KEYS:
                return self._json(400, {"error": "key not allowed"})
            code, out, err = hc("keys", t, key)
        else:
            return self._json(404, {"error": "not found"})
        if code:
            return self._json(502, {"error": (err or out).strip() or "failed"})
        self._json(200, {"ok": True})


class BridgeHTTPServer(ThreadingHTTPServer):
    def service_actions(self):
        # Also notice relay configuration changes while no phones are making requests.
        self.runtime.reload_relay()


def serve():
    if not HOST:
        raise SystemExit('No Tailscale address found. Start Tailscale or configure host.')
    if not current_token():
        print('WARNING: no token configured; API access is open to allowed network peers. Run pair to secure it.')
    print(f'Config: {config_path()}')
    runtime = Runtime()
    try:
        with BridgeHTTPServer((HOST, PORT), Handler) as httpd:
            httpd.runtime = runtime
            runtime.start()
            print(f'Herdr Remote bridge on http://{HOST}:{PORT}')
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        runtime.close()
