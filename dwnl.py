#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, os, re, sys, time, json, subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter, Retry
from bs4 import BeautifulSoup

# Опционально: Playwright для "живого" перехвата сетки
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT = True
except Exception:
    PLAYWRIGHT = False

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Веб-ID приложения Instagram (дефолт для web-клиента)
DEFAULT_IG_APP_ID = "936619743392459"  # можно переопределить флагом --ig-app-id
DEFAULT_DOC_ID = "10015901848480474"   # актуальный на 2025; можно переопределить флагом --doc-id

SHORTCODE_RE = re.compile(
    r"(?:instagram\.com/(?:p|reel|tv)/|^)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)

@dataclass
class DownloadItem:
    url: str
    filename: str

def extract_shortcode(url_or_code: str) -> str:
    m = SHORTCODE_RE.search(url_or_code)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_code):
        return url_or_code
    raise ValueError("Не удалось извлечь shortcode из URL/кода (11 символов).")

def make_session(proxy: Optional[str], timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",
        "Origin": "https://www.instagram.com",
    })
    retries = Retry(
        total=5, connect=5, read=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD", "POST"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s.request_timeout = timeout
    return s

def safe_filename(name: str) -> str:
    return re.sub(r"[^\w\.-]+", "_", name)

def download_file(s: requests.Session, url: str, outpath: str) -> None:
    with s.get(url, stream=True, timeout=s.request_timeout) as r:
        r.raise_for_status()
        tmp = outpath + ".part"
        os.makedirs(os.path.dirname(outpath), exist_ok=True)
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, outpath)

# ----------------- LSD extractor -----------------

_LSD_PATTERNS = [
    r'"LSD",\s*\[\],\s*{\s*"token"\s*:\s*"([^"]+)"\s*}',
    r'"LSD",\s*null,\s*{\s*"token"\s*:\s*"([^"]+)"\s*}',
    r'"lsd"\s*:\s*{\s*"token"\s*:\s*"([^"]+)"\s*}',
    r'name="lsd"\s+value="([^"]+)"',
]

def extract_lsd_token_from_html(html: str) -> Optional[str]:
    for pat in _LSD_PATTERNS:
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def get_lsd_token(s: requests.Session, shortcode: str, verbose: bool=False) -> Tuple[Optional[str], Optional[str]]:
    """Возвращает (lsd_token, referer_url) или (None, None)"""
    candidates = [
        f"https://www.instagram.com/reel/{shortcode}/",
        f"https://www.instagram.com/p/{shortcode}/",
        f"https://www.instagram.com/tv/{shortcode}/",
    ]
    for url in candidates:
        try:
            r = s.get(url, timeout=s.request_timeout, allow_redirects=True)
            if r.status_code >= 400:
                continue
            token = extract_lsd_token_from_html(r.text)
            if verbose:
                print(f"[*] LSD referer={url} status={r.status_code} token_found={bool(token)}", file=sys.stderr)
            if token:
                return token, url
        except requests.RequestException:
            time.sleep(1.0)
            continue
    return None, None

# ----------------- GraphQL fetch (без куки) -----------------

def choose_best_video_version(versions: list) -> Optional[str]:
    """Выбираем лучший URL из video_versions/video_resources (по bitrate/width)"""
    best = None
    best_score = -1
    for v in versions or []:
        # IG бывает дает ключи 'url', 'src'
        url = v.get("url") or v.get("src")
        if not isinstance(url, str):
            continue
        # оценка качества
        w = v.get("width") or 0
        h = v.get("height") or 0
        br = v.get("bit_rate") or v.get("bitrate") or 0
        score = int(br) or (int(w) * int(h))
        if score > best_score:
            best = url
            best_score = score
    return best

def parse_dash_manifest_for_baseurl(xml_text: str) -> Optional[str]:
    if not xml_text:
        return None
    # Часто в манифесте есть <BaseURL>https://...mpd</BaseURL>
    m = re.search(r"<BaseURL>\s*([^<]+)\s*</BaseURL>", xml_text)
    if m:
        return m.group(1).strip()
    # Альтернативно могут быть прямые сегменты/playlist-и
    m2 = re.search(r"https?://[^\s\"']+\.mpd", xml_text)
    if m2:
        return m2.group(0)
    m3 = re.search(r"https?://[^\s\"']+\.m3u8", xml_text)
    if m3:
        return m3.group(0)
    return None

def extract_media_from_xdt(node: dict, outdir: str, shortcode: str) -> List[DownloadItem]:
    items: List[DownloadItem] = []
    typename = node.get("__typename") or node.get("typename")
    base = os.path.join(outdir, safe_filename(shortcode))

    def add_url(u: str, idx: int = 1):
        ext = ".mp4" if ".mp4" in u.lower() else ".m3u8" if ".m3u8" in u.lower() else ".mpd" if ".mpd" in u.lower() else ".bin"
        name = base + (f"_{idx}" if idx > 1 else "") + ext
        items.append(DownloadItem(url=u, filename=name))

    # одиночное видео
    if node.get("is_video") or typename in ("XDTGraphVideo", "GraphVideo"):
        if node.get("video_url"):
            add_url(node["video_url"])
            return items
        if node.get("video_versions") or node.get("video_resources"):
            best = choose_best_video_version(node.get("video_versions") or node.get("video_resources") or [])
            if best:
                add_url(best)
                return items
        # DASH/HLS
        dash = node.get("video_dash_manifest") or node.get("clips_metadata", {}).get("video_dash_manifest")
        if dash:
            mpd = parse_dash_manifest_for_baseurl(dash)
            if mpd:
                add_url(mpd)
                return items
        playback = node.get("playback_url") or node.get("clips_metadata", {}).get("playback_url")
        if playback:
            add_url(playback)
            return items

    # карусель
    edges = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
    i = 0
    for e in edges:
        nd = (e or {}).get("node") or {}
        if not nd.get("is_video"):
            continue
        i += 1
        if nd.get("video_url"):
            add_url(nd["video_url"], i)
            continue
        if nd.get("video_versions") or nd.get("video_resources"):
            best = choose_best_video_version(nd.get("video_versions") or nd.get("video_resources") or [])
            if best:
                add_url(best, i)
                continue
        dash = nd.get("video_dash_manifest") or nd.get("clips_metadata", {}).get("video_dash_manifest")
        if dash:
            mpd = parse_dash_manifest_for_baseurl(dash)
            if mpd:
                add_url(mpd, i)
                continue
        playback = nd.get("playback_url") or nd.get("clips_metadata", {}).get("playback_url")
        if playback:
            add_url(playback, i)
            continue

    return items

def fetch_via_graphql(s: requests.Session, shortcode: str, outdir: str, ig_app_id: str, doc_id: str, verbose: bool=False) -> List[DownloadItem]:
    """
    1) GET reel page -> извлекаем LSD токен
    2) POST /api/graphql?doc_id=<doc_id> с X-FB-LSD и X-IG-App-ID
    3) Разбираем data.xdt_shortcode_media
    """
    lsd, referer = get_lsd_token(s, shortcode, verbose=verbose)
    if not lsd:
        if verbose:
            print("[!] Не удалось извлечь LSD токен из HTML.", file=sys.stderr)
        return []

    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-FB-LSD": lsd,
        "X-IG-App-ID": ig_app_id,
        "X-ASBD-ID": "129477",
        "Referer": referer or f"https://www.instagram.com/reel/{shortcode}/",
        "Origin": "https://www.instagram.com",
    }

    variables = {"shortcode": shortcode}
    payload = {
        "doc_id": doc_id,
        "variables": json.dumps(variables, separators=(",", ":")),
    }

    url = "https://www.instagram.com/api/graphql"
    r = s.post(url, headers=headers, data=payload, timeout=s.request_timeout)
    if r.status_code >= 400:
        if verbose:
            print(f"[!] GraphQL HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []

    try:
        data = r.json()
    except Exception:
        if verbose:
            print("[!] Не JSON ответ от GraphQL.", file=sys.stderr)
        return []

    node = ((data.get("data") or {}).get("xdt_shortcode_media")) or {}
    if not node:
        if verbose:
            print("[!] Нет data.xdt_shortcode_media в ответе.", file=sys.stderr)
        return []

    items = extract_media_from_xdt(node, outdir, shortcode)
    if verbose:
        print(f"[*] GraphQL нашёл {len(items)} медиа URL.", file=sys.stderr)
    return items

# ----------------- Playwright fallback -----------------

def fetch_via_playwright(shortcode: str, output: str, proxy: Optional[str], timeout_ms: int = 20000, verbose: bool=False) -> List[DownloadItem]:
    if not PLAYWRIGHT:
        return []

    embed_candidates = [
        f"https://www.instagram.com/reel/{shortcode}/embed",
        f"https://www.instagram.com/reel/{shortcode}/embed/captioned",
        f"https://www.instagram.com/p/{shortcode}/embed",
        f"https://www.instagram.com/p/{shortcode}/embed/captioned",
    ]

    items: List[DownloadItem] = []

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        launch_args = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        }
        if proxy:
            launch_args["proxy"] = {"server": proxy}

        browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = ctx.new_page()

        media_url = None
        media_size = -1

        def consider(url: str, headers: dict):
            nonlocal media_url, media_size
            u = url.lower()
            if any(x in u for x in (".mp4", ".m3u8", ".mpd")) and any(d in u for d in ("instagram", "fbcdn", "scontent")):
                if verbose:
                    print(">>", url, file=sys.stderr)
                size = 0
                try:
                    size = int(headers.get("content-length", "0"))
                except Exception:
                    pass
                if size >= media_size:
                    media_size = size
                    media_url = url

        def on_response(resp):
            try:
                consider(resp.url, resp.headers)
            except Exception:
                pass

        page.on("response", on_response)

        try:
            for url in embed_candidates:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.locator("video").first.click(timeout=1500)
                except Exception:
                    pass
                try:
                    page.evaluate("document.querySelector('video')?.play()?.catch(()=>{})")
                except Exception:
                    pass
                page.wait_for_timeout(5000)
                try:
                    vsrc = page.evaluate("() => document.querySelector('video')?.currentSrc || document.querySelector('video source')?.src || ''")
                    if vsrc and vsrc.startswith("http"):
                        consider(vsrc, {})
                except Exception:
                    pass
                if media_url:
                    break
        finally:
            ctx.close()
            browser.close()

        if not media_url:
            return []

        base = os.path.join(output, safe_filename(shortcode))
        ext = ".mp4" if media_url.lower().endswith(".mp4") else ".m3u8" if ".m3u8" in media_url.lower() else ".mpd"
        items.append(DownloadItem(url=media_url, filename=base + ext))
        return items

# ----------------- HLS/DASH handler -----------------

def handle_stream(item: DownloadItem):
    if ".m3u8" in item.url or ".mpd" in item.url:
        mp4_out = item.filename.rsplit(".", 1)[0] + ".mp4"
        print(f"[*] ffmpeg: {item.url} → {mp4_out}")
        rc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", item.url, "-c", "copy", mp4_out],
            capture_output=True, text=True
        )
        if rc.returncode == 0 and os.path.exists(mp4_out):
            print(f"[✓] Готово: {mp4_out}")
            sys.exit(0)
        else:
            print(f"[!] ffmpeg не смог собрать поток: {rc.stderr}", file=sys.stderr)

# ----------------- main -----------------

def main():
    ap = argparse.ArgumentParser(description="Скачивание публичных Instagram-видео БЕЗ логина/передачи своих куки.")
    ap.add_argument("url", help="URL поста/рила или сам shortcode (например, DEWKjVHsYUb)")
    ap.add_argument("-o", "--output", default="downloads", help="Папка для сохранения (по умолчанию downloads)")
    ap.add_argument("--proxy", default=None, help="HTTP(S) proxy, например http://host:port")
    ap.add_argument("--timeout", type=int, default=30, help="Таймаут запросов, сек")
    ap.add_argument("--ig-app-id", default=DEFAULT_IG_APP_ID, help="X-IG-App-ID (по умолчанию web id)")
    ap.add_argument("--doc-id", default=DEFAULT_DOC_ID, help="doc_id GraphQL запроса")
    ap.add_argument("-v", "--verbose", action="store_true", help="Подробный вывод")
    args = ap.parse_args()

    try:
        shortcode = extract_shortcode(args.url)
    except ValueError as e:
        print(f"[X] {e}", file=sys.stderr); sys.exit(2)

    os.makedirs(args.output, exist_ok=True)
    s = make_session(args.proxy, args.timeout)

    # 1) GraphQL без куки (с LSD токеном)
    if args.verbose:
        print("[*] Пытаюсь через GraphQL без логина…", file=sys.stderr)
    items = fetch_via_graphql(s, shortcode, args.output, args.ig_app_id, args.doc_id, verbose=args.verbose)

    # 2) Fallback: Playwright
    if not items and PLAYWRIGHT:
        print("[*] GraphQL не дал ссылок, пробую Playwright embed…", file=sys.stderr)
        items = fetch_via_playwright(shortcode, args.output, args.proxy, timeout_ms=args.timeout * 1000, verbose=args.verbose)

    if not items:
        print("[X] Не удалось получить медиа URL (пост может быть приватным/удалённым, либо IG снова поменял API).", file=sys.stderr)
        sys.exit(1)

    # 3) Скачивание
    for it in items:
        if ".m3u8" in it.url or ".mpd" in it.url:
            handle_stream(it)
            continue
        try:
            print(f"[*] Скачиваю: {it.url}")
            download_file(s, it.url, it.filename)
            print(f"[✓] Готово: {it.filename}")
            sys.exit(0)
        except Exception as e:
            print(f"[X] Ошибка скачивания {it.url}: {e}", file=sys.stderr)
            # пробуем следующий item (если есть)
            continue

    # если ни один не скачался
    sys.exit(1)

if __name__ == "__main__":
    main()
