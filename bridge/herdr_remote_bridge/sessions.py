"""Map a herdr terminal to its agent's transcript.

Claude Code writes ~/.claude/sessions/<pid>.json ({"sessionId", "cwd", ...}) for each
running process, so the mapping is exact: walk the terminal's process tree, find a Claude
process with a session file, and read its session id. Codex has no such file, so a
Codex terminal is matched by process start time and working folder against the
`session_meta` record at the top of each ~/.codex/sessions rollout.

Process lists come from `ps` on macOS/Linux, or from Windows (through PowerShell) when the
bridge runs in WSL but herdr runs on Windows: then pids, paths and homes are Windows ones.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import time

_CACHE = {}


def _cached(key, ttl, make):
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = make()
    _CACHE[key] = (now, value)
    return value


def _parse_etime(text):
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    days, _, rest = text.strip().rpartition('-')
    parts = [int(p) for p in rest.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return (int(days) if days else 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def windows_processes():
    """{pid: (ppid, name, start_ms)} for Windows, via PowerShell (works from WSL too)."""
    script = ("Get-CimInstance Win32_Process | ForEach-Object { '{0} {1} {2} {3}' -f $_.ProcessId, $_.ParentProcessId, "
              "[int64](($_.CreationDate.ToUniversalTime() - [datetime]'1970-01-01').TotalMilliseconds), $_.Name }")
    out = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                         capture_output=True, text=True, errors='replace', timeout=20,
                         cwd='/mnt/c' if os.path.isdir('/mnt/c') else None).stdout
    procs = {}
    for line in out.splitlines():
        bits = line.strip().split(' ', 3)
        if len(bits) == 4 and bits[0].isdigit() and bits[1].isdigit():
            try:
                procs[int(bits[0])] = (int(bits[1]), bits[3], int(bits[2]))
            except ValueError:
                continue
    return procs


def posix_processes():
    out = subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,etime=,comm='], capture_output=True, text=True,
                         errors='replace', timeout=20).stdout
    now = int(time.time() * 1000)
    procs = {}
    for line in out.splitlines():
        bits = line.split(None, 3)
        if len(bits) == 4 and bits[0].isdigit() and bits[1].isdigit():
            try:
                procs[int(bits[0])] = (int(bits[1]), os.path.basename(bits[3]), now - _parse_etime(bits[2]) * 1000)
            except ValueError:
                continue
    return procs


def processes(windows):
    return _cached(('procs', windows), 5, windows_processes if windows else posix_processes)


def descendants(roots, procs):
    """The given pids plus every process below them."""
    children = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    seen, stack = set(), [p for p in roots if p]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def claude_session(roots, home, procs):
    """{'sessionId', 'cwd'} of the Claude Code process running under these pids, or None.
    When several are nested (an agent running claude itself), the outermost one wins."""
    tree = descendants(roots, procs)
    found = []
    for pid in tree:
        path = Path(home) / '.claude' / 'sessions' / f'{pid}.json'
        try:
            info = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if info.get('sessionId') and info.get('pid', pid) == pid:
            found.append((procs.get(pid, (0, '', 0))[2], info))
    return min(found, key=lambda x: x[0])[1] if found else None


def _same_folder(a, b, local=None):
    """Same folder? Compares text first, then the folders on disk, which also catches
    Windows short names (C:\\Users\\JOHNSM~1 vs C:\\Users\\John Smith).
    `local` turns a path into one this process can open (wslpath -u under WSL)."""
    norm = lambda p: str(p or '').replace('\\', '/').rstrip('/').casefold()
    if norm(a) == norm(b):
        return True
    try:
        to_local = local or (lambda p: p)
        return os.path.samefile(to_local(a), to_local(b))
    except (OSError, ValueError):
        return False


def _rollout_meta(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            first = json.loads(stream.readline() or '{}')
    except (OSError, ValueError):
        return None
    payload = first.get('payload') if first.get('type') == 'session_meta' else None
    return payload if isinstance(payload, dict) else None


def _iso_ms(value):
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        return None


def codex_rollout(roots, home, cwd, procs, local=None):
    """Path of the Codex rollout for the Codex process under these pids, or None.
    A rollout belongs to that process when it started in the same folder after the
    process started and before any later Codex process started. Newest such rollout
    wins (a /new in the same process starts a fresh rollout)."""
    tree = descendants(roots, procs)
    mine = [procs[p][2] for p in tree if p in procs and 'codex' in procs[p][1].lower()]
    if not mine:
        return None
    start = min(mine)
    later = [s for p, (_, name, s) in procs.items() if 'codex' in name.lower() and s > start and p not in tree]
    until = min(later) if later else None
    best = None
    root = Path(home) / '.codex' / 'sessions'
    for path in root.rglob('rollout-*.jsonl') if root.is_dir() else []:
        try:
            if path.stat().st_mtime * 1000 < start - 5000:
                continue  # last written before this Codex started
        except OSError:
            continue
        meta = _rollout_meta(path)
        began = meta and _iso_ms(meta.get('timestamp'))
        if not began or began < start - 5000 or (until and began >= until):
            continue
        if cwd and meta.get('cwd') and not _same_folder(meta.get('cwd'), cwd, local):
            continue
        if best is None or began > best[0]:
            best = (began, path)
    return best[1] if best else None


def is_wsl():
    return platform.system() == 'Linux' and 'microsoft' in platform.release().lower()
