"""Framework-independent transcript, photo and folder helpers for Herd."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import uuid
import zlib

try:
    from .herd_push import caller_dir
except ImportError:
    from herd_push import caller_dir


def _tool(name, args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if isinstance(args, dict):
        args = next((args[k] for k in ('command', 'file_path', 'path', 'query', 'pattern') if k in args), json.dumps(args, ensure_ascii=False))
    return f'{name}({str(args)[:240]})'


def parse_transcript(path, since=None):
    messages = []
    with Path(path).open(encoding='utf-8', errors='replace') as stream:
        for index, line in enumerate(stream):
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # Includes a partially written trailing record.
            kind = entry.get('type')
            msg = entry.get('message', {}) if kind in ('user', 'assistant') else entry.get('payload', {})
            if not isinstance(msg, dict):
                continue
            if kind in ('user', 'assistant'):
                role = msg.get('role', kind)
            elif kind == 'response_item' and msg.get('type') in ('message', 'function_call'):
                role = msg.get('role', 'tool')
            else:
                continue
            if role not in ('user', 'assistant', 'tool'):
                continue
            content = msg.get('content', [])
            if isinstance(content, str):
                content = [{'type': 'text', 'text': content}]
            texts, tools = [], []
            if msg.get('type') == 'function_call':
                tools.append(_tool(msg.get('name', 'Tool'), msg.get('arguments', {})))
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get('type') in ('text', 'input_text', 'output_text') and isinstance(block.get('text'), str):
                    texts.append(block['text'])
                elif block.get('type') == 'tool_use':
                    tools.append(_tool(block.get('name', 'Tool'), block.get('input', {})))
            identity = str(entry.get('uuid') or entry.get('id') or msg.get('id') or
                           hashlib.sha256((str(index) + ':' + line).encode()).hexdigest()[:24])
            for suffix, item_role, text in [('text', role, '\n'.join(texts))] + [(f'tool-{i}', 'tool', t) for i, t in enumerate(tools)]:
                if not text:
                    continue
                record = {'id': identity + ':' + suffix, 'role': item_role, 'text': text}
                ts = entry.get('timestamp') or entry.get('ts')
                if isinstance(ts, str):
                    record['ts'] = ts
                messages.append(record)
    if since:
        offset = next((i + 1 for i, m in enumerate(messages) if m['id'] == since), None)
        if offset is not None:
            messages = messages[offset:]
    return messages[-200:]


def find_transcript(agent, cwd=None, session_file=None, session_id=None, home=None):
    """Prefer an explicit session file. Newest fallback is safe only for a sole terminal."""
    if session_file:
        path = Path(session_file).expanduser()
        if not path.is_file():
            raise ValueError('session file does not exist')
        return path
    home = Path(home) if home else Path.home()
    if agent == 'claude-code':
        if not cwd:
            raise ValueError('cwd or session_file required')
        encoded = re.sub(r'[^a-zA-Z0-9-]', '-', str(cwd))
        root = home / '.claude' / 'projects' / encoded
        candidates = list(root.glob('*.jsonl'))
        if session_id:
            candidates = [p for p in candidates if p.stem == session_id]
    elif agent == 'codex':
        candidates = list((home / '.codex' / 'sessions').rglob('rollout-*.jsonl'))
        if not session_id:
            raise ValueError('Codex requires session_file or session_id')
        candidates = [p for p in candidates if p.stem.endswith('-' + session_id)]
    else:
        raise ValueError('unsupported transcript agent')
    if not candidates:
        raise FileNotFoundError('no matching transcript')
    return max(candidates, key=lambda p: p.stat().st_mtime)


def handle_chat(data, *, agent, cwd=None, session_file=None, session_id=None, home=None):
    try:
        path = find_transcript(agent, cwd, session_file, session_id, home)
        return 200, {'messages': parse_transcript(path, data.get('since'))}
    except FileNotFoundError:
        return 404, {'error': 'no matching transcript'}
    except (ValueError, OSError):
        return 400, {'error': 'transcript unavailable; check terminal session mapping'}


def sanitize_filename(filename):
    if not isinstance(filename, str) or not filename or '/' in filename or '\\' in filename or '..' in filename or ':' in filename:
        raise ValueError('invalid filename or path traversal')
    name = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename).strip(' .')
    if Path(name).suffix.lower() not in ('.jpg', '.jpeg', '.png'):
        raise ValueError('only jpg/png uploads allowed')
    if len(name) > 180 or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}:
        raise ValueError('invalid filename')
    return name


def _image_valid(raw, suffix):
    # Structural validation without third-party image decoders; never decompress pixels.
    if suffix == '.png':
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            return False
        offset, first, image = 8, True, False
        while offset + 12 <= len(raw):
            length = int.from_bytes(raw[offset:offset + 4], 'big')
            kind = raw[offset + 4:offset + 8]
            end = offset + 8 + length
            if end + 4 > len(raw):
                return False
            chunk = raw[offset + 8:end]
            if zlib.crc32(kind + chunk) & 0xffffffff != int.from_bytes(raw[end:end + 4], 'big'):
                return False
            if first:
                if kind != b'IHDR' or length != 13 or not all(struct.unpack('>II', chunk[:8])):
                    return False
                first = False
            image |= kind == b'IDAT' and length > 0
            offset = end + 4
            if kind == b'IEND':
                return length == 0 and image and offset == len(raw)
        return False
    if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        return False
    # Walk JPEG segments through SOF and SOS, checking dimensions and bounds.
    offset, frame = 2, False
    while offset + 4 <= len(raw):
        if raw[offset] != 255:
            return False
        while offset < len(raw) and raw[offset] == 255:
            offset += 1
        if offset >= len(raw):
            return False
        marker = raw[offset]
        offset += 1
        length = int.from_bytes(raw[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(raw):
            return False
        if marker in (0xc0, 0xc1, 0xc2):
            frame = length >= 8 and bool(int.from_bytes(raw[offset + 3:offset + 5], 'big')) and bool(int.from_bytes(raw[offset + 5:offset + 7], 'big'))
        if marker == 0xda:
            return frame and offset + length < len(raw) - 2
        offset += length
    return False


def handle_upload(data, directory=None):
    try:
        if not isinstance(data, dict):
            raise ValueError('expected JSON object')
        name = sanitize_filename(data.get('filename'))
        encoded = data.get('data')
        limit = 15 * 1024 * 1024
        if not isinstance(encoded, str):
            raise ValueError('data must be base64')
        if len(encoded) > ((limit + 2) // 3) * 4:
            return 413, {'error': 'upload exceeds 15 MB'}
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > limit:
            return 413, {'error': 'upload exceeds 15 MB'}
        if not _image_valid(raw, Path(name).suffix.lower()):
            raise ValueError('invalid image content')
        directory = Path(directory) if directory else Path.home() / 'herd-uploads'
        directory.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids overwriting earlier uploads or following symlinks.
        path = directory.resolve() / name
        try:
            with path.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            path = directory.resolve() / (uuid.uuid4().hex + '-' + name)
            with path.open('xb') as stream:
                stream.write(raw)
        return 200, {'path': str(path)}
    except (ValueError, binascii.Error):
        return 400, {'error': 'invalid upload filename, base64 or image content'}
    except OSError:
        return 500, {'error': 'could not save upload'}


def validated_dir(value=None, root=None):
    home = Path.home().resolve()
    root = Path(root).resolve() if root else Path(home.anchor)
    path = Path(value).expanduser() if value else home
    if not path.is_absolute():
        raise ValueError('absolute folder path required')
    path = path.resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError('folder does not exist or is outside browse root')
    return path, root


def handle_dirs(data, root=None):
    try:
        path, root = validated_dir(data.get('path'), root)
        hidden = {'appdata', 'windows', 'program files', 'program files (x86)', 'programdata', '$recycle.bin', 'system volume information', 'recovery'}
        dirs = []
        for item in path.iterdir():
            if item.name.startswith('.') or item.name.lower() in hidden:
                continue
            try:
                info = item.stat()
                if getattr(info, 'st_file_attributes', 0) & 6:
                    continue
                if item.is_dir() and item.resolve().is_relative_to(root):
                    dirs.append(item.name)
            except OSError:
                continue
        return 200, {'path': str(path), 'parent': str(path.parent) if path != root else None, 'dirs': sorted(dirs, key=str.casefold)}
    except (ValueError, OSError):
        return 400, {'error': 'folder unavailable or outside browse root'}


class SpaceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else caller_dir() / 'herd-spaces.json'
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def set(self, name, cwd):
        if not isinstance(name, str) or not name.strip():
            raise ValueError('space name required')
        path, _ = validated_dir(cwd)
        with self.lock:
            self.data[name.strip()] = str(path)
            temp = self.path.with_suffix('.tmp')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
            os.replace(temp, self.path)

    def get(self, name):
        with self.lock:
            return next((cwd for key, cwd in self.data.items() if key.casefold() == name.casefold()), None)

    def spaces(self, existing=()):
        with self.lock:
            merged = {name.casefold(): {'name': name, 'cwd': cwd} for name, cwd in self.data.items()}
            for space in existing:
                record = {'name': space} if isinstance(space, str) else dict(space)
                saved = merged.get(record['name'].casefold(), {})
                record['cwd'] = record.get('cwd') or saved.get('cwd') or ''
                merged[record['name'].casefold()] = record
            return sorted(merged.values(), key=lambda s: s['name'].casefold())


def handle_space(data, store):
    try:
        if not isinstance(data, dict) or not isinstance(data.get('cwd'), str) or not data['cwd']:
            raise ValueError('space and cwd required')
        store.set(data.get('space'), data['cwd'])
        return 200, {'ok': True}
    except ValueError:
        return 400, {'error': 'valid space name and existing absolute cwd required'}
