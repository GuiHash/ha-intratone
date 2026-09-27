"""Regenerate custom_components/intratone/assets/call_placeholder.vp8.

The raw VP8 keyframe the audio bridge feeds ffmpeg until the intercom's real
video arrives (see `_VP8_PLACEHOLDER_KEYFRAME` in audio_bridge.py). Same look
as `doorbell_idle.jpg`, at the 640x480 output canvas.

Needs Pillow + an ffmpeg with libvpx; the font path is macOS'.

    python3 dev/make_call_placeholder.py
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_ASSETS = Path(__file__).parents[1] / "custom_components" / "intratone" / "assets"
_FONT = "/System/Library/Fonts/HelveticaNeue.ttc"
_W, _H = 640, 480


def main() -> None:
    im = Image.new("RGB", (_W, _H), (28, 32, 43))
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 0, _W, 40], fill=(44, 50, 64))
    draw.text(
        (_W / 2, 225), "Call in progress",
        font=ImageFont.truetype(_FONT, 44), fill=(222, 224, 236), anchor="mm",
    )
    draw.text(
        (_W / 2, 272), "Waiting for video…",
        font=ImageFont.truetype(_FONT, 22), fill=(140, 146, 166), anchor="mm",
    )
    with tempfile.TemporaryDirectory() as tmp:
        png, ivf = Path(tmp) / "placeholder.png", Path(tmp) / "placeholder.ivf"
        im.save(png)
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(png), "-frames:v", "1", "-pix_fmt", "yuv420p",
                "-c:v", "libvpx", "-qmin", "20", "-qmax", "20", "-b:v", "1M",
                "-f", "ivf", str(ivf),
            ],
            check=True,
        )
        data = ivf.read_bytes()
    # IVF: 32-byte file header, then per frame a 12-byte header whose first
    # 4 bytes are the little-endian frame size.
    size = int.from_bytes(data[32:36], "little")
    out = _ASSETS / "call_placeholder.vp8"
    out.write_bytes(data[44 : 44 + size])
    print(f"wrote {out} ({size} bytes)")


if __name__ == "__main__":
    main()
