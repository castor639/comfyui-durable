import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
import e2e  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "demo_out")
KILL_AT = 55
STEPS = 120


def relaunch_log():
    try:
        with open(os.path.join(e2e.STATE, "relaunch.log"), errors="ignore") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def main():
    os.makedirs(OUT, exist_ok=True)
    e2e.reset_state()
    e2e.start(extra=("--preview-method", "latent2rgb"))
    prompt = e2e.sd("demo", steps=STEPS, sampler="euler", seed=7, size=768)
    marks = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=OUT, record_video_size={"width": 1280, "height": 720})
        page = ctx.new_page()
        t0 = time.time()

        def mark(name):
            marks[name] = round(time.time() - t0, 2)
            print(name, marks[name], flush=True)

        page.goto(e2e.URL)
        page.wait_for_function("window.app && window.app.graph", timeout=60000)
        page.wait_for_timeout(2500)
        page.keyboard.press("Escape")
        page.evaluate("p => window.app.loadApiJson(p, 'durable-demo')", prompt)
        page.wait_for_timeout(1500)
        page.evaluate("window.app.extensionManager.setting.set('Comfy.Minimap.Visible', false)")
        page.evaluate("""() => {
            const c = window.app.canvas, n = window.app.graph.getNodeById(3), s = 0.85;
            c.ds.scale = s;
            c.ds.offset[0] = 1280 * 0.36 / s - (n.pos[0] + n.size[0] / 2);
            c.ds.offset[1] = 95 / s - n.pos[1];
            c.setDirty(true, true);
        }""")
        page.wait_for_timeout(1500)
        mark("ready")

        page.get_by_role("button", name="Run").first.click()
        mark("run_clicked")

        e2e.wait_snapshot(KILL_AT)
        for pid in e2e.comfy_pids():
            os.kill(pid, signal.SIGKILL)
        mark("killed")

        while not e2e.comfy_pids():
            time.sleep(0.2)
        mark("relaunched")
        e2e.wait_ready()
        mark("server_back")

        while "Resumed sampling at step" not in relaunch_log():
            time.sleep(0.2)
        mark("resumed")

        while "Prompt executed" not in relaunch_log():
            time.sleep(0.2)
        mark("finished")
        page.wait_for_timeout(5000)
        mark("end")
        video = page.video.path()
        ctx.close()
        browser.close()

    os.replace(video, os.path.join(OUT, "raw.webm"))
    with open(os.path.join(OUT, "marks.json"), "w") as f:
        json.dump(marks, f, indent=2)
    e2e.stop()


if __name__ == "__main__":
    main()
