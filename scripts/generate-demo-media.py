"""Rebuild the bundled synthetic posters and eight-second test clips."""

from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw


web = Path(__file__).resolve().parents[1] / "src/afterhours/web"
ffmpeg = shutil.which("ffmpeg")
if not ffmpeg:
    raise SystemExit("FFmpeg is required to regenerate the demo clips.")

for name, poster, color in (("rivet", "performer.png", "#32bb9d"),
                            ("nova", "nova-placeholder.jpg", "#cf8b46")):
    (web / "assets").mkdir(exist_ok=True)
    (web / "media").mkdir(exist_ok=True)
    image = Image.new("RGB", (640, 360), "#182123")
    draw = ImageDraw.Draw(image)
    for radius in range(22, 140, 15):
        draw.ellipse((320-radius, 171-radius, 320+radius, 171+radius), outline=color, width=2)
    draw.ellipse((309, 160, 331, 182), fill=color)
    draw.text((24, 330), "AFTERHOURS  /  SYNTHETIC PREVIEW", fill="#e6efea")
    image.save(web / "assets" / poster)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1",
                    "-i", str(web / "assets" / poster), "-t", "8", "-r", "24",
                    "-vf", "scale=320:180,format=yuv420p", "-c:v", "libx264",
                    "-movflags", "+faststart", "-an", str(web / "media" / f"{name}-demo.mp4")],
                   check=True)
