import asyncio
import hashlib
import logging
import os
import queue
import threading

import torch

from .store import replace
from comfy_api.latest._caching import CacheProvider, CacheValue

log = logging.getLogger("durable")

PLAIN = (int, float, bool, str, type(None))


def plain_size(obj):
    if isinstance(obj, torch.Tensor):
        return obj.numel() * obj.element_size()
    if isinstance(obj, PLAIN):
        return 0
    if isinstance(obj, (list, tuple)):
        total = 0
        for v in obj:
            s = plain_size(v)
            if s is None:
                return None
            total += s
        return total
    if isinstance(obj, dict):
        total = 0
        for k, v in obj.items():
            s = plain_size(v) if isinstance(k, str) else None
            if s is None:
                return None
            total += s
        return total
    return None


def to_cpu(obj):
    if isinstance(obj, torch.Tensor):
        return obj.detach().to("cpu")
    if isinstance(obj, list):
        return [to_cpu(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(to_cpu(v) for v in obj)
    if isinstance(obj, dict):
        return {k: to_cpu(v) for k, v in obj.items()}
    return obj


class DiskCache(CacheProvider):
    def __init__(self, root, controller, max_bytes, max_entry_bytes):
        self.dir = os.path.join(root, "cache")
        os.makedirs(self.dir, exist_ok=True)
        self.controller = controller
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.lock = threading.Lock()
        self.entries = {}
        for name in os.listdir(self.dir):
            p = os.path.join(self.dir, name)
            if name.endswith(".tmp"):
                os.remove(p)
            elif name.endswith(".pt"):
                self.entries[name[:-3]] = os.path.getsize(p)
        self.pending = queue.Queue()
        threading.Thread(target=self._writer, daemon=True, name="durable-cache").start()

    def _key(self, context):
        salt = self.controller.salt(context.node_id)
        if salt is None:
            return None
        return hashlib.sha256(f"{context.cache_key_hash}|{salt}".encode()).hexdigest()

    def _path(self, key):
        return os.path.join(self.dir, f"{key}.pt")

    def should_cache(self, context, value=None):
        if value is None:
            self.controller.record_node_hash(context.node_id, context.cache_key_hash)
        if self.controller.is_output_node(context.class_type):
            return False
        if value is not None:
            size = plain_size(value.outputs)
            if size is None or size > self.max_entry_bytes:
                return False
            key = self._key(context)
            if key is not None and key not in self.entries:
                self.pending.put((key, to_cpu(value.outputs)))
            return False
        return True

    def on_prompt_start(self, prompt_id):
        self.controller.prompt_started(prompt_id)

    async def on_lookup(self, context):
        key = self._key(context)
        if key is None or key not in self.entries:
            return None
        path = self._path(key)
        try:
            data = await asyncio.to_thread(torch.load, path, map_location="cpu", weights_only=True)
            os.utime(path)
        except Exception as e:
            log.warning(f"[durable] dropping unreadable cache entry for {context.class_type}: {e}")
            self._drop(key)
            return None
        self.controller.note_cache_hit(context.node_id, context.class_type)
        return CacheValue(outputs=data["outputs"], ui=None)

    async def on_store(self, context, value):
        pass

    def _writer(self):
        while True:
            key, outputs = self.pending.get()
            try:
                self._write(key, outputs)
            except Exception as e:
                log.warning(f"[durable] cache write failed: {e}")

    def _write(self, key, outputs):
        path = self._path(key)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        torch.save({"outputs": outputs}, tmp)
        replace(tmp, path)
        with self.lock:
            self.entries[key] = os.path.getsize(path)
        self._evict()

    def _drop(self, key):
        with self.lock:
            self.entries.pop(key, None)
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def _evict(self):
        with self.lock:
            total = sum(self.entries.values())
            if total <= self.max_bytes:
                return
            by_age = sorted(self.entries, key=lambda k: os.path.getmtime(self._path(k)))
        target = self.max_bytes * 0.9
        for key in by_age:
            if total <= target:
                break
            total -= self.entries.get(key, 0)
            self._drop(key)

    def usage(self):
        with self.lock:
            return len(self.entries), sum(self.entries.values())
