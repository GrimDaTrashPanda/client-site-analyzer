#!/usr/bin/env python3
"""
Palette Builder
- Reads colors.txt (and optionally samples images in assets/)
- KMeans cluster to top-N colors
- Exports: palette.json, palette.css, palette.scss, palette.csv, palette_preview.png
"""
import re, json, math, argparse
from pathlib import Path
from typing import List, Tuple
import numpy as np
from sklearn.cluster import KMeans
from PIL import Image

HEX_RE = re.compile(r"#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")

def clamp(v, lo=0, hi=255): return max(lo, min(hi, v))
def rgb_to_hex(rgb): return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

def parse_hex(token: str) -> Tuple[int,int,int]:
    t = token.lstrip("#")
    if len(t) in (3,4):
        r,g,b = [int(c*2,16) for c in t[:3]]
    elif len(t) in (6,8):
        r,g,b = int(t[0:2],16), int(t[2:4],16), int(t[4:6],16)
    else:
        raise ValueError
    return (r,g,b)

def load_color_tokens(colors_file: Path) -> List[Tuple[int,int,int]]:
    rgbs = []
    for line in colors_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line: continue
        hexes = HEX_RE.findall(line)
        for h in hexes:
            try:
                rgbs.append(parse_hex("#"+h))
            except: pass
        if not hexes and line.startswith("#"):
            try: rgbs.append(parse_hex(line))
            except: pass
    uniq = list(dict.fromkeys(rgbs))
    return uniq

def sample_images(folder: Path, max_pixels=400_000) -> List[Tuple[int,int,int]]:
    out = []
    if not folder.exists(): return out
    for p in folder.glob("**/*"):
        if p.suffix.lower() not in [".png",".jpg",".jpeg",".webp",".gif",".bmp",".tiff"]:
            continue
        try:
            im = Image.open(p).convert("RGB")
            w,h = im.size
            scale = math.sqrt(max_pixels / float(w*h)) if (w*h) > max_pixels else 1.0
            if scale < 1.0:
                im = im.resize((max(1,int(w*scale)), max(1,int(h*scale))), Image.BILINEAR)
            arr = np.array(im).reshape(-1,3)
            if arr.shape[0] > 10000:
                idx = np.random.choice(arr.shape[0], 10000, replace=False)
                arr = arr[idx]
            out.extend([tuple(map(int,px)) for px in arr.tolist()])
        except Exception:
            continue
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--colors-file", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--sample-images", default=None)
    args = ap.parse_args()

    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    tokens = load_color_tokens(Path(args.colors_file))
    if args.sample_images:
        tokens += sample_images(Path(args.sample_images))
    if not tokens:
        raise SystemExit("No colors found.")

    data = np.array(tokens, dtype=np.float32)
    k = min(args.k, len(tokens))
    km = KMeans(n_clusters=k, n_init=6, random_state=42).fit(data)
    centers = km.cluster_centers_.astype(int)

    def brightness(rgb): return 0.2126*rgb[0]+0.7152*rgb[1]+0.0722*rgb[2]
    ordered = sorted(centers.tolist(), key=brightness, reverse=True)

    palette = [{"hex": rgb_to_hex(tuple(map(int,c))), "rgb": list(map(int,c))} for c in ordered]
    (out_dir / "palette.json").write_text(json.dumps(palette, indent=2), encoding="utf-8")

    css = [":root {"]
    scss = []
    csv = ["index,hex,r,g,b"]
    for i, p in enumerate(palette, start=1):
        css.append(f"  --color-{i:02d}: {p['hex']};")