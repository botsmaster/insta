import asyncio
import json
import re
from playwright.async_api import async_playwright
from urllib.parse import urlparse, urljoin
import requests

class InstagramVideoParser:
    def __init__(self):
        self.manifest_urls = []
        self.segment_urls = []
        self.mp4_urls = []
        self.api_data = []
        
    async def parse_video(self, url):
        """Основной метод для парсинга видео из Instagram"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox'
                ]
            )
            
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080}
            )
            
            page = await context.new_page()
            
            # Обработчик ответов
            async def handle_response(response):
                url = response.url
                
                # 1. Ищем HLS манифесты (.m3u8)
                if '.m3u8' in url:
                    print(f"\n[HLS] Найден манифест: {url}")
                    self.manifest_urls.append({
                        'type': 'hls',
                        'url': url
                    })
                    
                    # Пытаемся получить содержимое манифеста
                    try:
                        content = await response.text()
                        self.parse_m3u8_content(content, url)
                    except:
                        pass
                
                # 2. Ищем DASH манифесты (.mpd)
                elif '.mpd' in url:
                    print(f"\n[DASH] Найден манифест: {url}")
                    self.manifest_urls.append({
                        'type': 'dash',
                        'url': url
                    })
                
                # 3. Прямые MP4 ссылки
                elif '.mp4' in url:
                    print(f"\n[MP4] Найден видео файл: {url[:100]}...")
                    self.mp4_urls.append(url)
                
                # 4. Ищем сегменты видео
                elif any(ext in url for ext in ['.ts', '.m4s', '.fmp4']):
                    self.segment_urls.append(url)
                
                # 5. API запросы с данными
                if '/api/v1/' in url or 'graphql' in url:
                    try:
                        if response.status == 200:
                            body = await response.text()
                            if body and '{' in body:
                                data = json.loads(body)
                                self.extract_media_from_json(data, url)
                    except:
                        pass
            
            page.on('response', handle_response)
            
            print(f"Открываем страницу: {url}")
            
            try:
                # Загружаем страницу
                response = await page.goto(url, wait_until='networkidle', timeout=30000)
                print(f"Статус ответа: {response.status}")
                
                # Ждем загрузки контента
                await page.wait_for_timeout(5000)
                
                # Пытаемся запустить видео кликом
                print("\nПытаемся запустить видео...")
                
                # Варианты селекторов для кнопки воспроизведения
                play_selectors = [
                    'button[aria-label*="Play"]',
                    'button[aria-label*="play"]',
                    'div[role="button"][aria-label*="Play"]',
                    'svg[aria-label*="Play"]',
                    'video',
                    'div[class*="video"]',
                    'article video',
                    'article button'
                ]
                
                for selector in play_selectors:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            await element.click()
                            print(f"Кликнули на элемент: {selector}")
                            await page.wait_for_timeout(3000)
                            break
                    except:
                        continue
                
                # Дополнительное ожидание для загрузки видео
                await page.wait_for_timeout(5000)
                
                # Извлекаем данные из страницы
                page_data = await self.extract_page_data(page)
                
            except Exception as e:
                print(f"Ошибка при загрузке страницы: {e}")
            
            await browser.close()
            
            # Анализируем результаты
            return self.analyze_results()
    
    def parse_m3u8_content(self, content, base_url):
        """Парсинг содержимого M3U8 манифеста"""
        lines = content.split('\n')
        base_dir = '/'.join(base_url.split('/')[:-1])
        
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                # Это URL сегмента
                if line.startswith('http'):
                    segment_url = line
                else:
                    segment_url = urljoin(base_dir + '/', line)
                
                self.segment_urls.append(segment_url)
                print(f"  Сегмент: {segment_url[:80]}...")
    
    def extract_media_from_json(self, data, source_url):
        """Извлечение медиа URL из JSON данных"""
        
        def search_json(obj, path=""):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    # Ключи, которые могут содержать видео URL
                    if key in ['video_url', 'src', 'url', 'playback_url', 'dash_manifest', 'hls_manifest']:
                        if isinstance(value, str) and value.startswith('http'):
                            print(f"\n[JSON] Найден {key}: {value[:80]}...")
                            if '.mp4' in value:
                                self.mp4_urls.append(value)
                            elif '.m3u8' in value:
                                self.manifest_urls.append({'type': 'hls', 'url': value})
                            elif '.mpd' in value:
                                self.manifest_urls.append({'type': 'dash', 'url': value})
                    
                    # Массив video_versions
                    if key == 'video_versions' and isinstance(value, list):
                        for version in value:
                            if isinstance(version, dict) and 'url' in version:
                                self.mp4_urls.append(version['url'])
                                print(f"\n[JSON] Video version: {version['url'][:80]}...")
                    
                    search_json(value, f"{path}.{key}")
                    
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    search_json(item, f"{path}[{i}]")
        
        search_json(data)
    
    async def extract_page_data(self, page):
        """Извлечение данных со страницы"""
        return await page.evaluate('''() => {
            const data = {
                videos: [],
                scripts: []
            };
            
            // Все video элементы
            document.querySelectorAll('video').forEach(video => {
                data.videos.push({
                    src: video.src,
                    currentSrc: video.currentSrc,
                    poster: video.poster,
                    sources: Array.from(video.querySelectorAll('source')).map(s => ({
                        src: s.src,
                        type: s.type
                    }))
                });
            });
            
            // Поиск window переменных
            if (window._sharedData) data.sharedData = window._sharedData;
            if (window.__initialData) data.initialData = window.__initialData;
            
            return data;
        }''')
    
    def analyze_results(self):
        """Анализ и вывод результатов"""
        print("\n" + "="*60)
        print("РЕЗУЛЬТАТЫ АНАЛИЗА")
        print("="*60)
        
        results = {
            'mp4_urls': list(set(self.mp4_urls)),
            'manifest_urls': self.manifest_urls,
            'segments_count': len(set(self.segment_urls)),
            'segments_sample': list(set(self.segment_urls))[:5]
        }
        
        # MP4 файлы
        if results['mp4_urls']:
            print(f"\n✅ Найдено прямых MP4 ссылок: {len(results['mp4_urls'])}")
            for i, url in enumerate(results['mp4_urls'][:3], 1):
                print(f"\n{i}. {url}")
        else:
            print("\n❌ Прямые MP4 ссылки не найдены")
        
        # Манифесты
        if results['manifest_urls']:
            print(f"\n✅ Найдено манифестов: {len(results['manifest_urls'])}")
            for manifest in results['manifest_urls']:
                print(f"\n{manifest['type'].upper()}: {manifest['url']}")
        else:
            print("\n❌ HLS/DASH манифесты не найдены")
        
        # Сегменты
        if results['segments_count'] > 0:
            print(f"\n✅ Найдено сегментов видео: {results['segments_count']}")
            print("\nПримеры сегментов:")
            for i, seg in enumerate(results['segments_sample'], 1):
                print(f"{i}. {seg[:80]}...")
        
        # Рекомендации
        print("\n" + "="*60)
        print("РЕКОМЕНДАЦИИ")
        print("="*60)
        
        if not results['mp4_urls'] and not results['manifest_urls']:
            print("\n❌ Instagram блокирует доступ к видео без авторизации")
            print("\n📋 Что можно попробовать:")
            print("1. Использовать cookie от авторизованной сессии")
            print("2. Попробовать мобильный API Instagram")
            print("3. Использовать сторонние сервисы загрузки")
            print("4. Проанализировать сетевой трафик в DevTools браузера")
        else:
            print("\n✅ Видео данные найдены!")
            print("\n📋 Как скачать видео:")
            
            if results['mp4_urls']:
                print("\n1. Для MP4 файлов:")
                print("   - Используйте requests с правильными заголовками")
                print("   - Может потребоваться передача cookies")
                print("   - Пример: requests.get(url, headers={'User-Agent': '...'})")
            
            if results['manifest_urls']:
                print("\n2. Для HLS/DASH манифестов:")
                print("   - Скачайте манифест")
                print("   - Извлеките список сегментов")
                print("   - Скачайте все сегменты")
                print("   - Объедините с помощью ffmpeg")
                print("   - Пример: ffmpeg -i manifest.m3u8 -c copy output.mp4")
        
        # Сохраняем результаты
        with open('instagram_video_results.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 Результаты сохранены в instagram_video_results.json")
        
        return results

async def main():
    url = "https://www.instagram.com/reel/DEWKjVHsYUb/"
    parser = InstagramVideoParser()
    results = await parser.parse_video(url)
    
    # Пример кода для загрузки
    if results['mp4_urls'] or results['manifest_urls']:
        print("\n" + "="*60)
        print("ПРИМЕР КОДА ДЛЯ ЗАГРУЗКИ")
        print("="*60)
        
        print("""
import requests

# Для MP4:
def download_mp4(url, filename):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    response = requests.get(url, headers=headers, stream=True)
    with open(filename, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

# Для HLS:
# pip install m3u8downloader
# m3u8downloader manifest.m3u8 -o video.mp4
        """)

if __name__ == "__main__":
    asyncio.run(main())