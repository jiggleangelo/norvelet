"""Single-process persistent capability workspaces.

Run one application process per DATA_DIR. Locks are fixed stripes, not a
registry indexed by untrusted capability tokens.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock, RLock


class NotFound(Exception):
    pass


class CapacityError(Exception):
    pass


class RateError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def token_hash(token):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', token):
        raise NotFound('Workspace not found')
    return hashlib.sha256(token.encode()).hexdigest()


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class WorkspaceStore:
    def __init__(self, data_dir, data_cap_mb=200, max_workspaces=3000):
        self.root = os.path.abspath(data_dir)
        self.workspace_root = os.path.join(self.root, 'workspaces')
        self.share_root = os.path.join(self.root, 'shares')
        self.tombstone_root = os.path.join(self.root, 'tombstones')
        self.budget_file = os.path.join(self.root, 'creation-budget.json')
        self.cap = int(data_cap_mb) * 1024 * 1024
        if self.cap <= 0 or max_workspaces < 1:
            raise ValueError('Storage limits must be positive')
        self.max_workspaces = max_workspaces
        self.workspace_cap = 5 * 1024 * 1024
        self.guard = RLock()
        self.locks = tuple(Lock() for _ in range(64))
        self.write_windows = {}
        for directory in (self.root, self.workspace_root, self.share_root, self.tombstone_root):
            os.makedirs(directory, mode=0o700, exist_ok=True)
        self.total = self._disk_total()
        self.last_sweep = 0
        self.sweep()

    def _lock(self, digest):
        return self.locks[int(hashlib.sha256(digest.encode()).hexdigest()[:8], 16) % len(self.locks)]

    def _file(self, digest):
        return os.path.join(self.workspace_root, digest, 'ws.json')

    def _disk_total(self):
        return sum(os.path.getsize(os.path.join(root, name))
                   for root, _, names in os.walk(self.root) for name in names)

    def _size(self, path):
        try:
            return os.path.getsize(path)
        except FileNotFoundError:
            return 0

    def _atomic(self, path, payload):
        old_size = self._size(path)
        if self.total - old_size + len(payload) > self.cap:
            raise CapacityError('Storage is full; export your work and try again later')
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.mkdir(parent, 0o700)
            fsync_directory(os.path.dirname(parent))
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=parent)
        replaced = False
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            replaced = True
            self.total += len(payload) - old_size
            fsync_directory(parent)
        finally:
            if not replaced:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _unlink(self, path):
        size = self._size(path)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        self.total -= size
        fsync_directory(os.path.dirname(path))

    def _save(self, digest, data):
        payload = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        if len(payload) > self.workspace_cap:
            raise CapacityError('A workspace is limited to 5 MB')
        self._atomic(self._file(digest), payload)

    def _read_hash(self, digest):
        try:
            with open(self._file(digest), encoding='utf-8') as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise NotFound('Workspace not found') from None
        if not isinstance(data, dict) or not isinstance(data.get('projects'), list):
            raise ValueError('Invalid workspace')
        lifetime = 90 * 86400 if data.get('written') else 86400
        if time.time() - os.path.getmtime(self._file(digest)) >= lifetime:
            self._remove(digest, data)
            raise NotFound('Workspace expired')
        return data

    def _creation_budget(self):
        timestamp = time.time()
        try:
            with open(self.budget_file, encoding='utf-8') as handle:
                entries = json.load(handle)
        except FileNotFoundError:
            entries = []
        entries = [entry for entry in entries if entry > timestamp - 3600]
        if len(entries) >= 60:
            raise RateError('Workspace creation limit reached; try again in an hour. Existing workspaces remain usable.')
        entries.append(timestamp)
        self._atomic(self.budget_file, json.dumps(entries).encode())

    def create(self, sample=False, *, rate_limited=False):
        # HTTP creation always opts into the persisted, global creation budget.
        # Direct administrative/test callers do not consume the public budget.
        with self.guard:
            self._maybe_sweep()
            if len(os.listdir(self.workspace_root)) >= self.max_workspaces:
                raise CapacityError('Workspace capacity reached')
            if rate_limited:
                self._creation_budget()
            token = secrets.token_urlsafe(24)
            digest = token_hash(token)
            stamp = now()
            data = {'schema': 1, 'created_at': stamp, 'updated_at': stamp,
                    'written': False, 'sample': bool(sample), 'share_hash': None, 'projects': []}
            try:
                self._save(digest, data)
            except Exception:
                directory = os.path.dirname(self._file(digest))
                if os.path.isdir(directory) and not os.listdir(directory):
                    os.rmdir(directory)
                    fsync_directory(self.workspace_root)
                raise
            return token, data

    def load(self, token):
        with self.guard:
            self._maybe_sweep()
            return self._read_hash(token_hash(token))

    def mutate(self, token, operation):
        digest = token_hash(token)
        with self._lock(digest), self.guard:
            self._maybe_sweep()
            data = self._read_hash(digest)
            stamp = time.monotonic()
            window = self.write_windows.setdefault(digest, deque())
            while window and window[0] <= stamp - 60:
                window.popleft()
            if len(window) >= 120:
                raise RateError('Too many workspace writes; try again in a minute')
            window.append(stamp)
            result = operation(data)
            data['written'] = True
            data['updated_at'] = now()
            self._save(digest, data)
            return result

    def rotate_share(self, token):
        digest = token_hash(token)
        with self._lock(digest), self.guard:
            data = self._read_hash(digest)
            timestamp = time.monotonic()
            window = self.write_windows.setdefault(digest, deque())
            while window and window[0] <= timestamp - 60:
                window.popleft()
            if len(window) >= 120:
                raise RateError('Too many workspace writes; try again in a minute')
            window.append(timestamp)
            share = secrets.token_urlsafe(24)
            share_digest = token_hash(share)
            old = data.get('share_hash')
            index = os.path.join(self.share_root, share_digest)
            self._atomic(index, digest.encode())
            try:
                data['share_hash'] = share_digest
                self._save(digest, data)
            except Exception:
                self._unlink(index)
                raise
            if old:
                self._unlink(os.path.join(self.share_root, old))
            return share

    def load_share(self, token):
        share_digest = token_hash(token)
        with self.guard:
            try:
                with open(os.path.join(self.share_root, share_digest), encoding='ascii') as handle:
                    digest = handle.read()
            except FileNotFoundError:
                raise NotFound('Share link not found') from None
            if not re.fullmatch('[a-f0-9]{64}', digest):
                raise NotFound('Share link not found')
            data = self._read_hash(digest)
            if data.get('share_hash') != share_digest:
                raise NotFound('Share link not found')
            return data

    def _remove(self, digest, data):
        share = data.get('share_hash')
        if share:
            self._unlink(os.path.join(self.share_root, share))
        self._unlink(self._file(digest))
        directory = os.path.dirname(self._file(digest))
        if os.path.isdir(directory):
            os.rmdir(directory)
            fsync_directory(self.workspace_root)
        self.write_windows.pop(digest, None)

    def delete(self, token):
        digest = token_hash(token)
        with self._lock(digest), self.guard:
            data = self._read_hash(digest)
            # Free the record first so deletion also works at the global cap.
            self._remove(digest, data)
            self._atomic(os.path.join(self.tombstone_root, digest), now().encode())

    def _maybe_sweep(self):
        if time.time() - self.last_sweep >= 86400:
            self.sweep()

    def sweep(self):
        with self.guard:
            timestamp = time.time()
            for digest in os.listdir(self.workspace_root):
                path = self._file(digest)
                try:
                    with open(path, encoding='utf-8') as handle:
                        data = json.load(handle)
                    lifetime = 90 * 86400 if data.get('written') else 86400
                    if timestamp - os.path.getmtime(path) >= lifetime:
                        self._remove(digest, data)
                except FileNotFoundError:
                    continue
                except (ValueError, TypeError, AttributeError):
                    continue
            # Tombstones contain only workspace digests and deletion timestamps.
            for name in os.listdir(self.tombstone_root):
                path = os.path.join(self.tombstone_root, name)
                if timestamp - os.path.getmtime(path) >= 90 * 86400:
                    self._unlink(path)
            self.last_sweep = timestamp
