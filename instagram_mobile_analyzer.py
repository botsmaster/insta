import asyncio
import json
import re
from playwright.async_api import async_playwright
from urllib.parse import unquote
import base64
import time

class InstagramMobileAnalyzer:
    def __init__(self):
        self.results = {
            'media_urls': [],
            'api_responses': [],
            'graphql_data': []
        }
        
    async def analyze_mobile(self, url):
        """Анализ через мобильный интерфейс Instagram"""
        async with async_playwright() as p:
            # Используем мобильный user-agent
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-features=IsolateOrigins,site-per-process'
                ]
            )
            
            # Мобильный контекст
            context = await browser.new_context(
                viewport={'width': 375, 'height': 812},
                user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True
            )
            
            page = await context.new_page()
            
            # Перехват запросов
            captured_urls = []
            
            async def handle_response(response):
                url = response.url
                
                # Ищем медиа файлы
                if any(ext in url for ext in ['.mp4', '.m3u8', 'video/', 'scontent']):
                    captured_urls.append({
                        'url': url,
                        'status': response.status,
                        'type': 'media'
                    })
                    print(f"[MEDIA] {url[:100]}...")
                
                # GraphQL API
                if 'graphql' in url or 'api/v1' in url:
                    try:
                        body = await response.text()
                        if body:
                            data = json.loads(body)
                            self.analyze_graphql_response(url, data)
                    except:
                        pass
                
                # Перехват Instagram API v1
                if '/api/v1/media/' in url:
                    try:
                        body = await response.text()
                        data = json.loads(body)
                        print(f"[API v1] Found media API response")
                        self.results['api_responses'].append({
                            'url': url,
                            'data': data
                        })
                    except:
                        pass
            
            page.on('response', handle_response)
            
            print(f"\nОткрываем мобильную версию: {url}")
            
            # Пробуем несколько вариантов URL
            urls_to_try = [
                url,
                url.replace('www.', 'm.'),  # Мобильная версия
                url.replace('www.instagram.com', 'i.instagram.com'),  # API endpoint
            ]
            
            for test_url in urls_to_try:
                try:
                    print(f"\nПробуем URL: {test_url}")
                    response = await page.goto(test_url, wait_until='networkidle', timeout=20000)
                    
                    if response.status == 200:
                        break
                except Exception as e:
                    print(f"Ошибка при загрузке {test_url}: {e}")
            
            # Ждем загрузки
            await page.wait_for_timeout(5000)
            
            # Попробуем кликнуть на видео для запуска
            try:
                # Ищем видео элемент
                video_element = await page.query_selector('video, div[role="button"][aria-label*="Play"], div[class*="video"]')
                if video_element:
                    await video_element.click()
                    print("Кликнули на видео элемент")
                    await page.wait_for_timeout(3000)
            except:
                pass
            
            # Извлекаем данные из страницы
            page_data = await page.evaluate('''() => {
                const data = {
                    videos: [],
                    scripts: [],
                    localStorage: {}
                };
                
                // Видео элементы
                document.querySelectorAll('video').forEach(video => {
                    data.videos.push({
                        src: video.src,
                        currentSrc: video.currentSrc,
                        poster: video.poster,
                        duration: video.duration,
                        readyState: video.readyState
                    });
                });
                
                // LocalStorage данные
                try {
                    for (let key in localStorage) {
                        if (key.includes('media') || key.includes('video')) {
                            data.localStorage[key] = localStorage[key];
                        }
                    }
                } catch(e) {}
                
                // Поиск данных в window
                if (window.__initialData) data.initialData = window.__initialData;
                if (window.__additionalData) data.additionalData = window.__additionalData;
                
                return data;
            }''')
            
            print("\n=== РЕЗУЛЬТАТЫ АНАЛИЗА ===")
            print(f"Найдено видео элементов: {len(page_data['videos'])}")
            print(f"Перехвачено медиа URL: {len(captured_urls)}")
            
            # Сохраняем HTML
            html = await page.content()
            with open('instagram_mobile_page.html', 'w', encoding='utf-8') as f:
                f.write(html)
            
            await browser.close()
            
            return {
                'captured_urls': captured_urls,
                'page_data': page_data,
                'results': self.results
            }
    
    def analyze_graphql_response(self, url, data):
        """Анализ GraphQL ответов"""
        
        def search_for_video(obj, path=""):
            if isinstance(obj, dict):
                # Известные поля с видео
                video_fields = [
                    'video_url', 'video_versions', 'video_dash_manifest',
                    'video_codec', 'video_duration', 'dash_manifest',
                    'playback_url', 'progressive_download_url'
                ]
                
                for key, value in obj.items():
                    if key in video_fields:
                        print(f"[GraphQL] Найдено поле {key} в {path}")
                        if isinstance(value, str) and value.startswith('http'):
                            self.results['media_urls'].append(value)
                        elif isinstance(value, list):
                            for item in value:
                                if isinstance(item, dict) and 'url' in item:
                                    self.results['media_urls'].append(item['url'])
                    
                    search_for_video(value, f"{path}.{key}")
                    
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    search_for_video(item, f"{path}[{i}]")
        
        search_for_video(data)

async def main():
    url = "https://www.instagram.com/reel/DEWKjVHsYUb/"
    
    print("=== АНАЛИЗ МОБИЛЬНОЙ ВЕРСИИ INSTAGRAM ===")
    analyzer = InstagramMobileAnalyzer()
    results = await analyzer.analyze_mobile(url)
    
    # Выводим результаты
    print("\n=== ИТОГОВЫЕ РЕЗУЛЬТАТЫ ===")
    
    all_media_urls = []
    
    # Медиа из перехваченных запросов
    for item in results['captured_urls']:
        if item['type'] == 'media':
            all_media_urls.append(item['url'])
    
    # Медиа из GraphQL
    all_media_urls.extend(results['results']['media_urls'])
    
    # Уникальные URL
    unique_urls = list(set(all_media_urls))
    
    print(f"\nВсего найдено уникальных медиа URL: {len(unique_urls)}")
    
    if unique_urls:
        print("\nНайденные медиа URL:")
        for i, url in enumerate(unique_urls[:5], 1):
            print(f"{i}. {url}")
    else:
        print("\n❌ Медиа URL не найдены")
        print("\nВОЗМОЖНЫЕ ПРИЧИНЫ:")
        print("1. Instagram блокирует доступ без авторизации")
        print("2. Используется защита от автоматизации")
        print("3. Видео загружается только после взаимодействия пользователя")
        
    # Сохраняем результаты
    with open('instagram_mobile_results.json', 'w', encoding='utf-8') as f:
        json.dump({
            'url': url,
            'media_urls': unique_urls,
            'captured_count': len(results['captured_urls']),
            'page_data': results['page_data']
        }, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    asyncio.run(main())