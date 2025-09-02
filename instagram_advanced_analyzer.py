import asyncio
import json
import re
from playwright.async_api import async_playwright
from urllib.parse import unquote, parse_qs, urlparse
import base64

class InstagramAdvancedAnalyzer:
    def __init__(self):
        self.page_data = {}
        self.network_logs = []
        self.media_info = {}
        
    async def analyze_reel(self, url):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled']
            )
            
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = await context.new_page()
            
            # Перехват всех запросов
            async def log_request(request):
                self.network_logs.append({
                    'url': request.url,
                    'method': request.method,
                    'headers': dict(request.headers)
                })
                
            async def handle_response(response):
                url = response.url
                
                # Логируем все медиа запросы
                if any(pattern in url for pattern in ['.mp4', '.m3u8', '.mpd', 'video/', '/v/', 'scontent']):
                    print(f"[MEDIA REQUEST] {url[:100]}...")
                    self.media_info[url] = {
                        'status': response.status,
                        'headers': dict(response.headers)
                    }
                
                # Перехватываем GraphQL ответы
                if 'graphql' in url or 'api' in url:
                    try:
                        text = await response.text()
                        if text:
                            data = json.loads(text)
                            self.analyze_api_response(url, data)
                    except:
                        pass
            
            page.on('request', log_request)
            page.on('response', handle_response)
            
            print(f"\nОткрываем: {url}")
            
            # Перехватываем консольные сообщения
            page.on('console', lambda msg: print(f"[CONSOLE] {msg.text}"))
            
            # Переходим на страницу
            response = await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Ждем загрузки контента
            await page.wait_for_timeout(5000)
            
            # Извлекаем данные из страницы
            print("\n=== ИЗВЛЕЧЕНИЕ ДАННЫХ СО СТРАНИЦЫ ===")
            
            # 1. Получаем все скрипты и данные
            page_data = await page.evaluate('''() => {
                const data = {
                    sharedData: window._sharedData || null,
                    additionalData: window.__additionalDataLoaded || null,
                    reactData: null,
                    videoElements: [],
                    scripts: []
                };
                
                // Поиск видео элементов
                document.querySelectorAll('video').forEach(video => {
                    data.videoElements.push({
                        src: video.src,
                        currentSrc: video.currentSrc,
                        poster: video.poster,
                        dataset: Object.assign({}, video.dataset)
                    });
                });
                
                // Поиск React данных
                const findReactFiber = (dom) => {
                    const key = Object.keys(dom).find(key => 
                        key.startsWith("__reactFiber$") || 
                        key.startsWith("__reactInternalInstance$")
                    );
                    return key ? dom[key] : null;
                };
                
                // Поиск данных в React компонентах
                document.querySelectorAll('*').forEach(el => {
                    const fiber = findReactFiber(el);
                    if (fiber && fiber.memoizedProps) {
                        const props = fiber.memoizedProps;
                        if (props.post || props.media || props.video) {
                            data.reactData = props;
                        }
                    }
                });
                
                // Собираем все inline скрипты
                document.querySelectorAll('script').forEach(script => {
                    if (script.innerHTML && script.innerHTML.includes('video')) {
                        data.scripts.push(script.innerHTML.substring(0, 1000));
                    }
                });
                
                return data;
            }''')
            
            self.page_data = page_data
            
            # 2. Анализируем полученные данные
            if page_data['sharedData']:
                print("\nНайден window._sharedData!")
                self.analyze_shared_data(page_data['sharedData'])
            
            # 3. Пытаемся найти видео через альтернативные методы
            print("\n=== ПОИСК ВИДЕО ЧЕРЕЗ DOM ===")
            
            # Ищем элементы с фоновыми изображениями (часто содержат превью видео)
            media_elements = await page.evaluate('''() => {
                const elements = [];
                document.querySelectorAll('[style*="background-image"], [style*="background: url"]').forEach(el => {
                    const style = el.getAttribute('style');
                    const urlMatch = style.match(/url\\(['"]?([^'"\\)]+)['"]?\\)/);
                    if (urlMatch && urlMatch[1]) {
                        elements.push(urlMatch[1]);
                    }
                });
                return elements;
            }''')
            
            print(f"Найдено элементов с медиа в стилях: {len(media_elements)}")
            
            # 4. Пытаемся кликнуть на видео для инициации загрузки
            print("\n=== ПОПЫТКА ВОСПРОИЗВЕДЕНИЯ ВИДЕО ===")
            try:
                # Ищем кнопку play или само видео
                play_button = await page.query_selector('button[aria-label*="Play"], [role="button"][aria-label*="Play"], svg[aria-label*="Play"]')
                if play_button:
                    await play_button.click()
                    print("Кликнули на кнопку воспроизведения")
                    await page.wait_for_timeout(3000)
                else:
                    # Пробуем кликнуть на область видео
                    video_area = await page.query_selector('div[role="button"], article video, article > div > div')
                    if video_area:
                        await video_area.click()
                        print("Кликнули на область видео")
                        await page.wait_for_timeout(3000)
            except:
                print("Не удалось кликнуть на видео")
            
            # 5. Финальная попытка - поиск в сетевых логах
            print("\n=== АНАЛИЗ СЕТЕВЫХ ЗАПРОСОВ ===")
            self.analyze_network_logs()
            
            # 6. Сохраняем HTML для дальнейшего анализа
            html = await page.content()
            with open('instagram_page.html', 'w', encoding='utf-8') as f:
                f.write(html)
            
            await browser.close()
            
            return self.get_results()
    
    def analyze_shared_data(self, data):
        """Анализ window._sharedData"""
        
        def extract_media(obj, path=""):
            if isinstance(obj, dict):
                # Ключи, которые могут содержать медиа
                media_keys = [
                    'video_url', 'video_src', 'src', 
                    'display_url', 'display_src',
                    'video_versions', 'image_versions2',
                    'dash_info', 'video_dash_manifest'
                ]
                
                for key, value in obj.items():
                    if key in media_keys and isinstance(value, str):
                        print(f"  Найден {key}: {value[:100]}...")
                        self.media_info[value] = {'source': f'sharedData.{path}.{key}'}
                    
                    # Рекурсивный поиск
                    extract_media(value, f"{path}.{key}")
                    
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    extract_media(item, f"{path}[{i}]")
        
        extract_media(data)
    
    def analyze_api_response(self, url, data):
        """Анализ GraphQL/API ответов"""
        
        def find_media_in_response(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in ['video_url', 'video_versions', 'dash_info']:
                        print(f"[API] Найден {key} в {url}")
                        if isinstance(value, str):
                            self.media_info[value] = {'source': f'API: {url}'}
                        elif isinstance(value, list):
                            for item in value:
                                if isinstance(item, dict) and 'url' in item:
                                    self.media_info[item['url']] = {'source': f'API: {url}'}
                    find_media_in_response(value)
            elif isinstance(obj, list):
                for item in obj:
                    find_media_in_response(item)
        
        find_media_in_response(data)
    
    def analyze_network_logs(self):
        """Анализ всех сетевых запросов"""
        print(f"Всего запросов: {len(self.network_logs)}")
        
        # Фильтруем медиа запросы
        media_requests = [
            log for log in self.network_logs 
            if any(pattern in log['url'] for pattern in [
                'scontent', 'cdninstagram', '.mp4', '.m3u8', 
                'video', '/v/', 'media', 'playback'
            ])
        ]
        
        print(f"Медиа запросов: {len(media_requests)}")
        
        for req in media_requests[:10]:
            print(f"  {req['method']}: {req['url'][:100]}...")
    
    def get_results(self):
        """Получение итоговых результатов"""
        results = {
            'media_urls': list(self.media_info.keys()),
            'page_data_found': bool(self.page_data.get('sharedData')),
            'video_elements': self.page_data.get('videoElements', []),
            'total_network_requests': len(self.network_logs)
        }
        
        # Сохраняем результаты
        with open('instagram_advanced_results.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        print("\n=== ИТОГОВЫЕ РЕЗУЛЬТАТЫ ===")
        print(f"Найдено медиа URL: {len(results['media_urls'])}")
        
        if results['media_urls']:
            print("\nНайденные медиа:")
            for i, url in enumerate(results['media_urls'][:5], 1):
                print(f"{i}. {url}")
                if url in self.media_info:
                    info = self.media_info[url]
                    print(f"   Источник: {info.get('source', 'Unknown')}")
        
        return results

async def main():
    url = "https://www.instagram.com/reel/DEWKjVHsYUb/"
    analyzer = InstagramAdvancedAnalyzer()
    results = await analyzer.analyze_reel(url)
    
    print("\n=== РЕКОМЕНДАЦИИ ===")
    if not results['media_urls']:
        print("❌ Прямые ссылки на видео не найдены.")
        print("\nВозможные причины:")
        print("1. Instagram требует авторизацию для доступа к медиа")
        print("2. Видео загружается динамически после взаимодействия")
        print("3. Используется защита от парсинга")
        print("\nРекомендации:")
        print("1. Использовать Instagram API с авторизацией")
        print("2. Использовать сторонние сервисы для загрузки")
        print("3. Анализировать мобильную версию сайта")
    else:
        print("✅ Найдены медиа URL!")
        print("\nДля загрузки видео:")
        print("1. Используйте найденные URL с правильными заголовками")
        print("2. Может потребоваться передача cookies")
        print("3. Некоторые URL могут быть временными")

if __name__ == "__main__":
    asyncio.run(main())