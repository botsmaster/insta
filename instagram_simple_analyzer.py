import requests
from bs4 import BeautifulSoup
import json
import re
from urllib.parse import unquote

def analyze_instagram_page(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }
    
    print(f"Fetching: {url}")
    response = requests.get(url, headers=headers)
    print(f"Status: {response.status_code}")
    
    if response.status_code != 200:
        print(f"Error: Got status code {response.status_code}")
        return
    
    html = response.text
    soup = BeautifulSoup(html, 'html.parser')
    
    print("\n=== ANALYZING HTML STRUCTURE ===")
    
    # 1. Поиск video тегов
    videos = soup.find_all('video')
    print(f"\nFound {len(videos)} video tags")
    for i, video in enumerate(videos):
        print(f"Video {i+1}:")
        print(f"  src: {video.get('src')}")
        print(f"  poster: {video.get('poster')}")
        
    # 2. Поиск source тегов
    sources = soup.find_all('source')
    print(f"\nFound {len(sources)} source tags")
    for source in sources:
        print(f"  src: {source.get('src')}")
        print(f"  type: {source.get('type')}")
    
    # 3. Поиск JSON-LD данных
    json_ld_scripts = soup.find_all('script', type='application/ld+json')
    print(f"\nFound {len(json_ld_scripts)} JSON-LD scripts")
    for script in json_ld_scripts:
        try:
            data = json.loads(script.string)
            if 'video' in str(data).lower():
                print("  Found video data in JSON-LD!")
                print(json.dumps(data, indent=2)[:500] + "...")
        except:
            pass
    
    # 4. Самое важное - поиск window._sharedData
    print("\n=== SEARCHING FOR INSTAGRAM DATA ===")
    scripts = soup.find_all('script')
    
    shared_data = None
    additional_data = None
    
    for script in scripts:
        if script.string:
            # window._sharedData
            if 'window._sharedData' in script.string:
                match = re.search(r'window\._sharedData\s*=\s*({.+?});', script.string, re.DOTALL)
                if match:
                    try:
                        shared_data = json.loads(match.group(1))
                        print("Found window._sharedData!")
                    except json.JSONDecodeError as e:
                        print(f"Error parsing _sharedData: {e}")
            
            # window.__additionalDataLoaded
            if 'window.__additionalDataLoaded' in script.string:
                matches = re.findall(r'window\.__additionalDataLoaded\([\'"]([^\'"]+)[\'"]\s*,\s*({.+?})\);', script.string, re.DOTALL)
                if matches:
                    additional_data = []
                    for path, data_str in matches:
                        try:
                            data = json.loads(data_str)
                            additional_data.append({'path': path, 'data': data})
                            print(f"Found window.__additionalDataLoaded for path: {path}")
                        except:
                            pass
    
    # 5. Анализ найденных данных
    media_urls = []
    
    if shared_data:
        print("\n=== ANALYZING SHARED DATA ===")
        media_urls.extend(extract_media_urls(shared_data))
    
    if additional_data:
        print("\n=== ANALYZING ADDITIONAL DATA ===")
        for item in additional_data:
            media_urls.extend(extract_media_urls(item['data']))
    
    # 6. Поиск прямых ссылок в HTML
    print("\n=== SEARCHING FOR DIRECT LINKS ===")
    
    # Поиск всех ссылок на CDN
    for tag in soup.find_all(attrs={'src': True}):
        src = tag.get('src')
        if src and ('scontent' in src or 'cdninstagram' in src):
            print(f"Found CDN src: {src[:100]}...")
            if '.mp4' in src:
                media_urls.append(src)
    
    for tag in soup.find_all(attrs={'href': True}):
        href = tag.get('href')
        if href and ('scontent' in href or 'cdninstagram' in href):
            print(f"Found CDN href: {href[:100]}...")
            if '.mp4' in href:
                media_urls.append(href)
    
    # Поиск в тексте скриптов
    for script in scripts:
        if script.string:
            # Поиск URL-адресов видео
            video_urls = re.findall(r'(https?://[^"\s]+\.mp4[^"\s]*)', script.string)
            for url in video_urls:
                clean_url = unquote(url).replace('\\u0026', '&')
                print(f"Found video URL in script: {clean_url[:100]}...")
                media_urls.append(clean_url)
            
            # Поиск URL-адресов HLS/DASH
            manifest_urls = re.findall(r'(https?://[^"\s]+\.(m3u8|mpd)[^"\s]*)', script.string)
            for url, ext in manifest_urls:
                clean_url = unquote(url).replace('\\u0026', '&')
                print(f"Found {ext} manifest: {clean_url[:100]}...")
                media_urls.append(clean_url)
    
    # 7. Вывод результатов
    print("\n=== RESULTS ===")
    unique_media_urls = list(set(media_urls))
    print(f"Total unique media URLs found: {len(unique_media_urls)}")
    
    for i, url in enumerate(unique_media_urls, 1):
        print(f"\n{i}. {url}")
    
    # Сохранение результатов
    with open('instagram_simple_analysis.json', 'w', encoding='utf-8') as f:
        json.dump({
            'url': url,
            'media_urls': unique_media_urls,
            'has_shared_data': shared_data is not None,
            'has_additional_data': additional_data is not None
        }, f, indent=2, ensure_ascii=False)
    
    return unique_media_urls

def extract_media_urls(obj, path=""):
    """Рекурсивно извлекает медиа URL из объекта"""
    urls = []
    
    if isinstance(obj, dict):
        for key, value in obj.items():
            # Известные ключи с медиа URL
            if key in ['video_url', 'display_url', 'display_src', 'src', 'video_src', 
                      'video_versions', 'display_resources', 'thumbnail_src']:
                if isinstance(value, str) and value.startswith('http'):
                    print(f"  Found {key} at {path}.{key}")
                    urls.append(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and 'url' in item:
                            urls.append(item['url'])
                            print(f"  Found {key}[].url at {path}.{key}")
            
            # Проверяем вложенные объекты
            urls.extend(extract_media_urls(value, f"{path}.{key}"))
            
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            urls.extend(extract_media_urls(item, f"{path}[{i}]"))
    
    return urls

if __name__ == "__main__":
    url = "https://www.instagram.com/reel/DEWKjVHsYUb/"
    media_urls = analyze_instagram_page(url)