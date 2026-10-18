"""Build a Windows .ico with transparent corners from the generated icon."""

from collections import deque
from pathlib import Path

from PIL import Image

SRC = Path(r"C:\Users\waben\.cursor\projects\c-Users-waben-llamacpp-cppllama\assets\llama-cpp-icon.jpg")
OUT_DIR = Path(r"C:\Users\waben\llamacpp\cppllama\launcher")
PNG = OUT_DIR / "llama.png"
ICO = OUT_DIR / "llama.ico"


def knock_out_white(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    w, h = im.size
    pix = im.load()

    def is_margin(x: int, y: int) -> bool:
        r, g, b, _a = pix[x, y]
        return r > 228 and g > 228 and b > 228 and abs(r - g) < 18 and abs(g - b) < 18

    seen = set()
    queue = deque([(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)])
    while queue:
        x, y = queue.popleft()
        if (x, y) in seen or x < 0 or y < 0 or x >= w or y >= h:
            continue
        if not is_margin(x, y):
            continue
        seen.add((x, y))
        pix[x, y] = (0, 0, 0, 0)
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    for _ in range(3):
        updates = []
        for y in range(h):
            for x in range(w):
                r, g, b, a = pix[x, y]
                if a == 0:
                    continue
                touches = False
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and pix[nx, ny][3] == 0:
                        touches = True
                        break
                if touches and r > 170 and g > 170 and b > 170:
                    lum = (r + g + b) / 3
                    alpha = int(max(0, min(255, (255 - lum) * 4)))
                    updates.append((x, y, r, g, b, alpha))
        for x, y, r, g, b, alpha in updates:
            pix[x, y] = (r, g, b, alpha)
    return im


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    im = knock_out_white(Image.open(SRC))
    side = min(im.size)
    left = (im.size[0] - side) // 2
    top = (im.size[1] - side) // 2
    im = im.crop((left, top, left + side, top + side))
    im.resize((256, 256), Image.Resampling.LANCZOS).save(PNG)
    base = im.resize((256, 256), Image.Resampling.LANCZOS)
    base.save(
        ICO,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"wrote {ICO} ({ICO.stat().st_size} bytes) and {PNG}")


if __name__ == "__main__":
    main()
