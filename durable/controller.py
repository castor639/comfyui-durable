import inspect
import logging
import os
import threading
import time
from collections import deque

from .store import ACTIVE, as_item

log = logging.getLogger("durable")

FILE_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx",
                 ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov", ".wav", ".mp3", ".flac")


def is_oom(error):
    text = f"{error.get('exception_type', '')} {error.get('exception_message', '')}".lower()
    return "outofmemory" in text or "out of memory" in text or "allocation on device" in text


class Controller:
    def __init__(self, server, store, oom_retries):
        self.server = server
        self.store = store
        self.oom_retries = oom_retries
        self.pause_requested = False
        self.pausing_queue = False
        self.sampling = False
        self.prompts = {}
        self.current_prompt = None
        self.node_hashes = {}
        self.sampler_calls = {}
        self.snapshots = {}
        self.salts = {}
        self.events = deque(maxlen=50)
        self.event_id = 0
        self.lock = threading.Lock()
        self.journal = None
        self.late_installs = []

    def event(self, kind, message, **data):
        with self.lock:
            self.event_id = max(self.event_id + 1, time.time_ns() // 1_000_000)
            ev = {"id": self.event_id, "kind": kind, "message": message, "time": time.time(), **data}
            self.events.append(ev)
        log.info(f"[durable] {message}")
        try:
            self.server.send_sync("durable.event", ev)
        except Exception:
            pass

    def is_output_node(self, class_type):
        import nodes
        cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
        return cls is None or bool(getattr(cls, "OUTPUT_NODE", False))

    # per prompt bookkeeping

    def prompt_started(self, prompt_id):
        while self.late_installs:
            try:
                self.late_installs.pop()()
            except Exception as e:
                log.warning(f"[durable] adapter failed to install: {e}")
        self.current_prompt = prompt_id
        self.node_hashes = {}
        self.sampler_calls = {}
        self.salts = {}

    def record_node_hash(self, node_id, cache_key_hash):
        self.node_hashes[node_id] = cache_key_hash

    def node_hash(self, prompt_id, node_id):
        if prompt_id != self.current_prompt:
            return None
        h = self.node_hashes.get(node_id)
        salt = self.salt(node_id)
        if h is None or salt is None:
            return None
        return f"{h}|{salt}"

    def next_sampler_call(self, prompt_id, node_id, list_index):
        k = (node_id, list_index)
        self.sampler_calls[k] = self.sampler_calls.get(k, -1) + 1
        return self.sampler_calls[k]

    def track_snapshot(self, prompt_id, key):
        self.snapshots.setdefault(prompt_id, set()).add(key)

    def note_cache_hit(self, node_id, class_type):
        log.info(f"[durable] restored {class_type} (node {node_id}) from disk cache")

    def salt(self, node_id):
        if node_id in self.salts:
            return self.salts[node_id]
        prompt = self.prompts.get(self.current_prompt)
        if prompt is None or node_id not in prompt:
            return None
        import comfyui_version
        parts, seen, stack = [comfyui_version.__version__], set(), [node_id]
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in prompt:
                continue
            seen.add(nid)
            node = prompt[nid]
            parts.append(f"{nid}:{node.get('class_type')}:{self._source_stamp(node.get('class_type'))}")
            for value in node.get("inputs", {}).values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    stack.append(value[0])
                elif isinstance(value, str) and value.lower().endswith(FILE_SUFFIXES):
                    parts.append(self._file_stamp(value))
        salt = "|".join(sorted(parts))
        self.salts[node_id] = salt
        return salt

    def _source_stamp(self, class_type):
        import nodes
        cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
        try:
            return os.stat(inspect.getsourcefile(cls)).st_mtime_ns
        except Exception:
            return 0

    def _file_stamp(self, name):
        import folder_paths
        candidates = [folder_paths.get_annotated_filepath(name)]
        for folder in list(folder_paths.folder_names_and_paths):
            candidates.append(folder_paths.get_full_path(folder, name))
        for path in candidates:
            if path and os.path.isfile(path):
                st = os.stat(path)
                return f"{name}:{st.st_size}:{st.st_mtime_ns}"
        return f"{name}:missing"

    # queue hooks

    def on_put(self, item):
        prompt_id = item[1]
        self.prompts[prompt_id] = item[2]
        self.store.save(item, "queued")

    def on_get(self, item):
        self.store.update(item[1], state="running", started=time.time())

    def on_removed(self, item):
        if self.pausing_queue:
            self.store.update(item[1], state="paused")
        else:
            self.store.remove(item[1])
            self.prompts.pop(item[1], None)

    def on_done(self, item, status):
        prompt_id = item[1]
        messages = dict((m[0], m[1]) for m in (status.messages or []) if isinstance(m, (list, tuple)) and len(m) == 2)
        if status.status_str == "success":
            self._finish(prompt_id)
            self.pause_requested = False
            return
        if "execution_interrupted" in messages:
            if self.pause_requested:
                self.store.update(prompt_id, state="paused")
                self.pause_requested = False
            else:
                self._finish(prompt_id)
            return
        error = messages.get("execution_error", {})
        job = self.store.get(prompt_id) or {}
        if is_oom(error) and job.get("oom_retries", 0) < self.oom_retries:
            self.store.update(prompt_id, oom_retries=job.get("oom_retries", 0) + 1)
            self.server.prompt_queue.set_flag("free_memory", True)
            self.event("retry", f"Out of memory in {error.get('node_type', 'a node')}, freed memory and retrying from the last checkpoint", prompt_id=prompt_id)
            threading.Timer(2.0, self.server.prompt_queue.put, args=(item,)).start()
            return
        self.store.remove(prompt_id)

    def _finish(self, prompt_id):
        self.store.remove(prompt_id)
        self.prompts.pop(prompt_id, None)
        if self.journal is not None:
            for key in self.snapshots.pop(prompt_id, ()):
                self.journal.discard(key)

    # pause and resume

    def pause(self):
        q = self.server.prompt_queue
        self.pausing_queue = True
        try:
            while q.delete_queue_item(lambda _: True):
                pass
        finally:
            self.pausing_queue = False
        running = list(q.currently_running.values())
        if not running:
            return
        self.pause_requested = True
        q.set_flag("free_memory", True)
        if not self.sampling:
            import comfy.model_management
            comfy.model_management.interrupt_current_processing(True)

    def resume(self):
        jobs = [j for j in self.store.all() if j["state"] == "paused"]
        self._requeue(jobs)
        if jobs:
            self.event("resumed_queue", f"Resumed {len(jobs)} paused job(s)")
        return len(jobs)

    def _requeue(self, jobs):
        if not jobs:
            return
        self.server.number = max(self.server.number, int(max(j["number"] for j in jobs)) + 1)
        for job in jobs:
            self.server.prompt_queue.put(as_item(job))

    def recover(self, clean_exit, max_attempts):
        jobs = [j for j in self.store.all() if j["state"] in ACTIVE + ("paused",)]
        if not jobs:
            return
        if clean_exit:
            for j in jobs:
                if j["state"] != "paused":
                    self.store.update(j["prompt_id"], state="paused")
            self.event("kept", f"Kept {len(jobs)} unfinished job(s) from the last session, press Resume to run them")
            return
        replay = []
        for j in jobs:
            if j["state"] == "paused":
                continue
            if j["state"] == "running":
                j = self.store.update(j["prompt_id"], attempts=j.get("attempts", 0) + 1)
                if j["attempts"] > max_attempts:
                    self.store.remove(j["prompt_id"])
                    self.event("gave_up", f"Job {j['prompt_id'][:8]} crashed ComfyUI {j['attempts']} times, dropped it")
                    continue
            replay.append(j)
        self._requeue(replay)
        if replay:
            self.event("recovered", f"ComfyUI stopped unexpectedly, requeued {len(replay)} job(s); they continue from their last checkpoint")

    def status(self, cache):
        counts = self.store.counts()
        entries, size = cache.usage() if cache else (0, 0)
        return {
            "jobs": counts,
            "paused": counts.get("paused", 0),
            "protected": counts.get("queued", 0) + counts.get("running", 0),
            "pausing": self.pause_requested,
            "cache": {"entries": entries, "bytes": size},
            "events": list(self.events),
        }


def install_queue_hooks(queue_cls, controller):
    put, get, task_done = queue_cls.put, queue_cls.get, queue_cls.task_done
    delete_queue_item, wipe_queue = queue_cls.delete_queue_item, queue_cls.wipe_queue

    def guarded(fn, *args):
        try:
            fn(*args)
        except Exception as e:
            log.warning(f"[durable] journal error: {e}")

    def put_hook(self, item):
        guarded(controller.on_put, item)
        return put(self, item)

    def get_hook(self, timeout=None):
        out = get(self, timeout)
        if out is not None:
            guarded(controller.on_get, out[0])
        return out

    def task_done_hook(self, item_id, history_result, status, *args, **kwargs):
        item = self.currently_running.get(item_id)
        out = task_done(self, item_id, history_result, status, *args, **kwargs)
        if item is not None and status is not None:
            guarded(controller.on_done, item, status)
        return out

    def delete_hook(self, function):
        removed = []

        def match(x):
            hit = function(x)
            if hit:
                removed.append(x)
            return hit

        out = delete_queue_item(self, match)
        for item in removed:
            guarded(controller.on_removed, item)
        return out

    def wipe_hook(self):
        with self.mutex:
            items = list(self.queue)
        out = wipe_queue(self)
        for item in items:
            guarded(controller.on_removed, item)
        return out

    queue_cls.put = put_hook
    queue_cls.get = get_hook
    queue_cls.task_done = task_done_hook
    queue_cls.delete_queue_item = delete_hook
    queue_cls.wipe_queue = wipe_hook
