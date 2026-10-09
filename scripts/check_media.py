"""Optional development check; requires Pillow, not an agent runtime dependency."""
from pathlib import Path
from PIL import Image, ImageSequence

root = Path(__file__).resolve().parents[1]
gif = Image.open(root / "docs/demo.gif")
duration = sum(frame.info.get("duration", 0) for frame in ImageSequence.Iterator(gif)) / 1000
print(f"GIF: {gif.n_frames} frames, {duration:.2f}s, {(root / 'docs/demo.gif').stat().st_size} bytes")
assert 30 <= duration <= 90, "Demo must be 30..90 seconds"
assert Image.open(root / "docs/social-preview.png").size == (1280, 640)
print("Media checks PASS")

