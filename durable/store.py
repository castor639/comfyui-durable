import json
import os
import threading
import time

ACTIVE = ("queued", "running")


def replace(src, dst, attempts=20):
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class JobStore:
    def __init__(self, root):
        self.root = root
        self.jobs_dir = os.path.join(root, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, prompt_id):
        return os.path.join(self.jobs_dir, f"{prompt_id}.json")

    def save(self, item, state, **extra):
        number, prompt_id, prompt, extra_data, outputs = item[:5]
        with self.lock:
            job = read_json(self._path(prompt_id), {})
            job.update(
                prompt_id=prompt_id,
                number=number,
                prompt=prompt,
                extra_data=extra_data,
                outputs=list(outputs),
                state=state,
                updated=time.time(),
            )
            job.setdefault("attempts", 0)
            job.setdefault("oom_retries", 0)
            job.update(extra)
            write_json(self._path(prompt_id), job)
            return job

    def get(self, prompt_id):
        return read_json(self._path(prompt_id))

    def update(self, prompt_id, **fields):
        with self.lock:
            job = read_json(self._path(prompt_id))
            if job is None:
                return None
            job.update(fields, updated=time.time())
            write_json(self._path(prompt_id), job)
            return job

    def remove(self, prompt_id):
        with self.lock:
            try:
                os.remove(self._path(prompt_id))
            except FileNotFoundError:
                pass

    def all(self):
        jobs = []
        for name in os.listdir(self.jobs_dir):
            if name.endswith(".json"):
                job = read_json(os.path.join(self.jobs_dir, name))
                if job:
                    jobs.append(job)
        return sorted(jobs, key=lambda j: j["number"])

    def counts(self):
        out = {}
        for job in self.all():
            out[job["state"]] = out.get(job["state"], 0) + 1
        return out


def as_item(job):
    return (job["number"], job["prompt_id"], job["prompt"], job["extra_data"], job["outputs"], {})
