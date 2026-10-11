import json
import os
import subprocess
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "demo_out"
raw = os.path.join(src, "raw.webm")
m = json.load(open(os.path.join(src, "marks.json")))
font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

segments = [
    (m["run_clicked"] - 1.0, m["killed"] - 1.0, 3.0, "1. A ComfyUI job runs. It has 120 steps."),
    (m["killed"] - 1.0, m["killed"] + 3.5, 1.0, "2. We stop ComfyUI with kill -9. This is the same as a crash."),
    (m["killed"] + 3.5, m["server_back"], 4.0, "3. Durable starts ComfyUI again by itself."),
    (m["server_back"], m["resumed"] + 5.5, 1.0, "4. The job continues at step 65. It does not go back to step 1."),
    (m["resumed"] + 5.5, m["finished"], 3.0, "5. The job continues to the last step."),
    (m["finished"], m["end"], 1.0, "6. The job is complete. No work is lost."),
]


def esc(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")


parts, labels = [], []
for i, (a, b, speed, caption) in enumerate(segments):
    tag = f"{speed:g}x speed" if speed != 1 else "real time"
    parts.append(
        f"[0:v]trim={a:.2f}:{b:.2f},setpts=(PTS-STARTPTS)/{speed},"
        f"drawbox=x=0:y=ih-92:w=iw:h=92:color=black@0.78:t=fill,"
        f"drawtext=fontfile={font}:text='{esc(caption)}':fontcolor=white:fontsize=30:x=(w-tw)/2:y=h-62,"
        f"drawtext=fontfile={font}:text='{tag}':fontcolor=white:fontsize=20:x=w-tw-20:y=h-130:box=1:boxcolor=black@0.6:boxborderw=8"
        f"[s{i}]"
    )
    labels.append(f"[s{i}]")
graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(segments)}:v=1:a=0[v]"

mp4 = os.path.join(src, "kill-test.mp4")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-filter_complex", graph, "-map", "[v]",
                "-c:v", "libx264", "-crf", "20", "-preset", "slow", "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", mp4], check=True)

gif = os.path.join(src, "kill-test.gif")
palette = os.path.join(src, "palette.png")
scale = "fps=10,scale=880:-1:flags=lanczos"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", mp4, "-vf", f"{scale},palettegen=max_colors=128:stats_mode=diff", palette], check=True)
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", mp4, "-i", palette, "-lavfi", f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", gif], check=True)

for path in (mp4, gif):
    print(path, round(os.path.getsize(path) / 1e6, 2), "MB")
