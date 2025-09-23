#!/usr/bin/env python3
"""
Sledgehammer Playwright Crawler (Complete)
- Renders with Chromium (headless by default)
- Single page OR depth-1 same-origin crawl (configurable)
- Captures: HTML, visible text, meta, CSS from network, computed colors, assets, full screenshot
- Builds LLM-ready text chunks
- Optional: Calls local Ollama to extract JSON insights
- Optional: Exports selector map CSV

Outputs -> crawl_output_playwright/
"""

import os, re, json, time, argparse, csv, urllib.parse
from pathlib import Path
from typing import Dict, List, Set, Tuple
from bs4 import BeautifulSoup
from tqdm import tqdm
import requests
from playwright.sync_api import sync_playwright

SAVE_DIR = Path("crawl_output_playwright")
ASSETS_DIR = SAVE_DIR / "assets"
CSS_DIR = SAVE_DIR / "css"
HEADERS = {"User-Agent": "web-audit-sledgehammer/1.0"}
COLOR_RE = re.compile(r"(#(?:[0-9a-fA-F]{3,8})\b|rgba?\([^)]+\)|hsla?\([^)]+\))")
CHUNK_SIZE_DEFAULT = 18000
CHUNK_OVERLAP = 800
RATE_LIMIT = 0.15

def ensure_dirs():
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    CSS_DIR.mkdir(parents=True, exist_ok=True)

def is_same_origin(base_url: str, candidate: str) -> bool:
    try:
        b = urllib.parse.urlparse(base_url)
        c = urllib.parse.urlparse(candidate)
        return (b.scheme, b.netloc) == (c.scheme, c.netloc)
    except Exception:
        return False

def norm(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)

def filename_from_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    base = os.path.basename(p.path) or "index"
    if p.query:
        base += f"_{abs(hash(p.query))%10**8}"
    return re.sub(r"[^0-9A-Za-z._-]", "_", base)

def chunk_text(big: str, chunk_size: int) -> List[Dict]:
    chunks = []
    i = 0
    L = len(big)
    while i < L:
        end = i + chunk_size
        chunk = big[max(0, i-CHUNK_OVERLAP):end]
        chunks.append({"index": len(chunks), "text": chunk})
        i = end
    return chunks

def extract_urls_from_css(css_text: str, base_url: str) -> List[str]:
    urls = []
    for m in re.finditer(r'url\(([^)]+)\)', css_text, re.I):
        raw = m.group(1).strip(' \'"')
        if not raw or raw.startswith("data:"):
            continue
        urls.append(norm(base_url, raw))
    for m in re.finditer(r'@import\s+(?:url\()?["\']?([^"\')]+)', css_text, re.I):
        raw = m.group(1).strip(' \'"')
        urls.append(norm(base_url, raw))
    return list(dict.fromkeys(urls))

def download_asset(url: str) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, stream=True)
        r.raise_for_status()
    except Exception as e:
        return {"url": url, "error": str(e)}
    name = filename_from_url(url)
    local = ASSETS_DIR / name
    try:
        with open(local, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk: f.write(chunk)
        return {
            "url": url,
            "local_path": str(local),
            "status": r.status_code,
            "content_type": r.headers.get("Content-Type"),
            "bytes": local.stat().st_size,
        }
    except Exception as e:
        return {"url": url, "error": f"save_failed: {e}"}

def render_and_capture(url: str, width: int, height: int, headless: bool=True) -> dict:
    ensure_dirs()
    css_blobs: Dict[str, str] = {}
    assets_from_css: List[str] = []
    responses_meta = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            viewport={"width": width, "height": height},
            user_agent=HEADERS["User-Agent"],
            java_script_enabled=True,
        )
        page = context.new_page()

        def on_response(resp):
            try:
                ctype = resp.headers.get("content-type", "")
                if "text/css" in ctype:
                    body = resp.text()
                    css_blobs[resp.url] = body
            except Exception:
                pass
            try:
                responses_meta.append({"url": resp.url, "status": resp.status, "content_type": resp.headers.get("content-type", "")})
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=60_000)

        # Scroll to force lazy loads
        page.evaluate("""
            async () => {
              await new Promise(resolve => {
                let total = 0;
                const step = () => {
                  window.scrollBy(0, 1000);
                  total += 1000;
                  if (total < document.body.scrollHeight + 2000) setTimeout(step, 50);
                  else { window.scrollTo(0,0); resolve(); }
                };
                step();
              });
            }""")
        page.wait_for_timeout(500)

        html = page.content()
        title = page.title()
        final_url = page.url
        screenshot_path = SAVE_DIR / "screenshot_full.png"
        page.screenshot(path=str(screenshot_path), full_page=True)

        # Visible text
        soup = BeautifulSoup(html, "html.parser")
        visible = "\n".join([t.strip() for t in soup.stripped_strings][:8000])

        # Meta tags
        metas = {}
        for m in soup.find_all("meta"):
            name = m.get("name") or m.get("property") or m.get("http-equiv")
            if name: metas[name] = m.get("content") or ""

        # DOM assets
        dom_assets = []
        for img in soup.find_all("img"):
            for srcattr in ("src","data-src"):
                src = img.get(srcattr) or ""
                if src and not src.startswith("data:"):
                    dom_assets.append(norm(final_url, src))
            srcset = img.get("srcset","")
            for part in srcset.split(","):
                u = part.strip().split(" ")[0]
                if u: dom_assets.append(norm(final_url, u))
        for ln in soup.find_all("link"):
            rel = " ".join(ln.get("rel", []))
            href = ln.get("href") or ""
            if href and any(k in rel for k in ["icon","shortcut","apple-touch-icon","manifest","preload","stylesheet"]):
                dom_assets.append(norm(final_url, href))
        dom_assets = list(dict.fromkeys(dom_assets))

        # Computed colors
        computed_rows = page.evaluate("""
            () => {
              const pick = ["color","backgroundColor","borderTopColor","borderRightColor","borderBottomColor","borderLeftColor","outlineColor","fill","stroke"];
              const els = Array.from(document.querySelectorAll("*")).slice(0, 1500);
              return els.map(el => {
                const cs = getComputedStyle(el);
                const colors = {};
                for (const p of pick) colors[p] = cs[p];
                return {tag: el.tagName.toLowerCase(), id: el.id || null, cls: el.className || "", colors};
              });
            }
        """)
        computed_colors = set()
        for row in computed_rows:
            for v in (row.get("colors") or {}).values():
                if not v: continue
                m = COLOR_RE.findall(v)
                for c in m: computed_colors.add(c.strip())

        # CSS URLs + colors
        css_colors = set()
        for css_url, css_text in css_blobs.items():
            (CSS_DIR / (filename_from_url(css_url) or "style.css")).write_text(css_text, encoding="utf-8")
            for c in COLOR_RE.findall(css_text): css_colors.add(c.strip())
            assets_from_css.extend(extract_urls_from_css(css_text, css_url))
        assets_from_css = list(dict.fromkeys(assets_from_css))

        # Download assets (best-effort)
        all_assets = list(dict.fromkeys(dom_assets + assets_from_css))
        if len(all_assets) > 250:
            all_assets = all_assets[:250]
        assets_meta = []
        for a in tqdm(all_assets, desc="downloading assets"):
            time.sleep(RATE_LIMIT)
            assets_meta.append(download_asset(a))

        # Write files
        (SAVE_DIR / "page.html").write_text(html, encoding="utf-8")
        (SAVE_DIR / "visible.txt").write_text(visible, encoding="utf-8")
        (SAVE_DIR / "final_url.txt").write_text(final_url, encoding="utf-8")
        (SAVE_DIR / "title.txt").write_text(title or "", encoding="utf-8")

        color_union = sorted(set(list(css_colors) + list(computed_colors)))
        (SAVE_DIR / "colors.txt").write_text("\n".join(color_union), encoding="utf-8")

        manifest = {
            "source_url": url,
            "final_url": final_url,
            "title": title,
            "counts": {
                "css_files": len(css_blobs),
                "assets": len(assets_meta),
                "colors_css": len(css_colors),
                "colors_computed": len(computed_colors),
            },
            "screenshot": str(screenshot_path),
            "meta": metas,
        }
        (SAVE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # Build big blob for LLM
        big = "\n\n".join([
            json.dumps(manifest, indent=2),
            "==== VISIBLE TEXT ====",
            visible,
            "==== COLORS ====",
            "\n".join(color_union),
            "==== HTML (truncated) ====",
            html[:30000],
            "==== FIRST CSS SNIPPETS ====",
            "\n\n".join([(t[:4000]) for t in list(css_blobs.values())[:5]]),
        ])

        browser.close()

    return {
        "html": html,
        "visible": visible,
        "manifest": manifest,
        "computed_rows": computed_rows,
        "chunks": chunk_text(big, CHUNK_SIZE_DEFAULT),
    }

def selector_map_to_csv(rows: List[dict], out_path: Path, max_rows: int = 3000):
    fields = ["tag", "id", "cls", "color","backgroundColor","borderTopColor","borderRightColor","borderBottomColor","borderLeftColor","outlineColor","fill","stroke"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in rows[:max_rows]:
            colors = r.get("colors", {})
            w.writerow([
                r.get("tag",""),
                r.get("id",""),
                r.get("cls",""),
                colors.get("color",""),
                colors.get("backgroundColor",""),
                colors.get("borderTopColor",""),
                colors.get("borderRightColor",""),
                colors.get("borderBottomColor",""),
                colors.get("borderLeftColor",""),
                colors.get("outlineColor",""),
                colors.get("fill",""),
                colors.get("stroke",""),
            ])

def call_llm_ollama(model: str, chunks: List[Dict], url="http://localhost:11434") -> dict:
    out = []
    sysmsg = "You analyze rendered web captures and MUST return strict JSON only."
    for c in chunks:
        prompt = f"""Return JSON with keys:
- page_summary (2-3 sentences),
- color_tokens (unique list),
- notable_selectors_or_elements (list),
- calls_to_action (list).

CHUNK:
{c['text'][:10000]}
"""
        payload = {
            "model": model,
            "messages": [
                {"role":"system","content":sysmsg},
                {"role":"user","content":prompt}
            ],
            "stream": False
        }
        r = requests.post(f"{url}/api/chat", json=payload, timeout=180)
        r.raise_for_status()
        msg = r.json().get("message", {}).get("content", "")
        try:
            out.append(json.loads(msg))
        except Exception:
            out.append({"raw": msg})
    (SAVE_DIR / "llm_results_ollama.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return {"per_chunk": out}

def crawl_depth1(start_url: str, max_pages: int, same_origin: bool, width: int, height: int, headless: bool=True):
    visited: Set[str] = set()
    queue: List[str] = [start_url]
    origin = urllib.parse.urlparse(start_url).netloc
    merged_colors: Set[str] = set()
    all_summaries: List[dict] = []

    pg = 0
    while queue and pg < max_pages:
        url = queue.pop(0)
        if url in visited: continue
        if same_origin and urllib.parse.urlparse(url).netloc != origin: continue
        visited.add(url); pg += 1
        print(f"[depth-1] crawling {pg}/{max_pages}: {url}")

        cap = render_and_capture(url, width, height, headless=headless)
        # merge colors
        colors = Path(SAVE_DIR / "colors.txt").read_text(encoding="utf-8").splitlines()
        merged_colors.update([c.strip() for c in colors if c.strip()])

        # discover links
        soup = BeautifulSoup(cap["html"], "html.parser")
        for a in soup.find_all("a", href=True):
            nxt = norm(url, a["href"])
            if nxt.startswith("mailto:") or nxt.startswith("javascript:"): continue
            if nxt not in visited and nxt not in queue:
                if (not same_origin) or is_same_origin(start_url, nxt):
                    queue.append(nxt)

        # write selector map CSV for this page
        selector_map_to_csv(cap["computed_rows"], SAVE_DIR / f"selector_map_{pg:03d}.csv")

    # write merged colors
    (SAVE_DIR / "colors_merged.txt").write_text("\n".join(sorted(merged_colors)), encoding="utf-8")
    # write visited list
    (SAVE_DIR / "visited_urls.txt").write_text("\n".join(sorted(visited)), encoding="utf-8")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--depth", type=int, default=0, help="0=single page, 1=follow same-origin links")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--same-origin", action="store_true")
    ap.add_argument("--ollama-model", default=None)
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--chunk-size", type=int, default=18000)
    ap.add_argument("--headless", type=str, default="True")  # "True" or "False"
    args = ap.parse_args()

    global CHUNK_SIZE_DEFAULT
    CHUNK_SIZE_DEFAULT = args.chunk_size
    headless_bool = (args.headless.lower() != "false")

    if args.depth == 0:
        cap = render_and_capture(args.url, args.width, args.height, headless=headless_bool)
        selector_map_to_csv(cap["computed_rows"], SAVE_DIR / "selector_map.csv")
        print(f"[done] chunks: {len(cap['chunks'])} -> {SAVE_DIR.resolve()}")
        if args.ollama_model:
            print("[info] calling Ollama…")
            call_llm_ollama(args.ollama_model, cap["chunks"], url=args.ollama_url)
            print("[done] wrote llm_results_ollama.json")
    else:
        crawl_depth1(args.url, args.max_pages, args.same_origin, args.width, args.height, headless=headless_bool)
        print(f"[done] depth-1 crawl -> {SAVE_DIR.resolve()}")