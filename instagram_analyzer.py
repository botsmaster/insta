import asyncio
import json
import re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from urllib.parse import unquote
import time

class InstagramAnalyzer:
    def __init__(self):
        self.media_urls = []
        self.api_responses = []
        self.network_requests = []
        
    async def analyze_page(self, url):
        async with async_playwright() as p:
            # Запускаем браузер
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = await context.new_page()
            
            # Перехватываем все сетевые запросы
            async def handle_request(request):
                self.network_requests.append({
                    'url': request.url,
                    'method': request.method,
                    'resource_type': request.resource_type
                })
                
            async def handle_response(response):
                url = response.url
                # Ищем медиа-файлы
                if any(ext in url for ext in ['.mp4', '.m3u8', '.mpd', 'video/']):
                    self.media_urls.append({
                        'url': url,
                        'status': response.status,
                        'content_type': response.headers.get('content-type', '')
                    })
                    print(f"[MEDIA] Found: {url}")
                
                # Ищем API ответы с данными
                if 'graphql' in url or 'api' in url or 'media' in url:
                    try:
                        body = await response.body()
                        text = body.decode('utf-8', errors='ignore')
                        if text and ('{' in text or '[' in text):
                            self.api_responses.append({
                                'url': url,
                                'body': text[:1000],  # Первые 1000 символов
                                'full_body': text
                            })
                            print(f"[API] Found: {url}")
                    except:
                        pass
            
            page.on('request', handle_request)
            page.on('response', handle_response)
            
            print(f"Opening: {url}")
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Ждем загрузки контента
            await page.wait_for_timeout(5000)
            
            # Получаем HTML
            html_content = await page.content()
            
            # Анализируем HTML
            print("\n=== ANALYZING HTML ===")
            self.analyze_html(html_content)
            
            # Анализируем скрипты
            print("\n=== ANALYZING SCRIPTS ===")
            await self.analyze_scripts(page)
            
            # Выводим результаты
            print("\n=== NETWORK ANALYSIS ===")
            self.print_network_analysis()
            
            await browser.close()
            
            return {
                'html': html_content,
                'media_urls': self.media_urls,
                'api_responses': self.api_responses
            }
    
    def analyze_html(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        
        # Ищем video теги
        videos = soup.find_all('video')
        print(f"Found {len(videos)} video tags")
        for video in videos:
            print(f"  src: {video.get('src')}")
            print(f"  poster: {video.get('poster')}")
            
        # Ищем source теги
        sources = soup.find_all('source')
        print(f"\nFound {len(sources)} source tags")
        for source in sources:
            print(f"  src: {source.get('src')}")
            print(f"  type: {source.get('type')}")
            
        # Ищем скрипты с данными
        scripts = soup.find_all('script', type='application/ld+json')
        print(f"\nFound {len(scripts)} JSON-LD scripts")
        for script in scripts:
            try:
                data = json.loads(script.string)
                if 'video' in str(data).lower():
                    print("  Found video data in JSON-LD!")
                    print(json.dumps(data, indent=2)[:500])
            except:
                pass
                
        # Ищем скрипты с window переменными
        all_scripts = soup.find_all('script')
        for script in all_scripts:
            if script.string and ('window._sharedData' in script.string or 'window.__additionalDataLoaded' in script.string):
                print("\nFound Instagram data script!")
                # Извлекаем JSON
                match = re.search(r'window\._sharedData\s*=\s*({.+?});', script.string)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        self.analyze_shared_data(data)
                    except:
                        pass
    
    def analyze_shared_data(self, data):
        print("\n=== ANALYZING SHARED DATA ===")
        
        # Рекурсивный поиск медиа URL
        def find_media_urls(obj, path=""):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in ['video_url', 'display_url', 'display_src', 'src', 'video_src']:
                        print(f"  Found {key} at {path}.{key}: {value}")
                    elif 'video' in key.lower() or 'media' in key.lower() or 'url' in key.lower():
                        if isinstance(value, str) and ('http' in value or '.mp4' in value):
                            print(f"  Potential media at {path}.{key}: {value[:100]}...")
                    find_media_urls(value, f"{path}.{key}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    find_media_urls(item, f"{path}[{i}]")
        
        find_media_urls(data)
    
    async def analyze_scripts(self, page):
        # Выполняем JavaScript для поиска видео элементов
        result = await page.evaluate('''() => {
            const results = {
                videos: [],
                mediaElements: [],
                reactProps: []
            };
            
            // Поиск video элементов
            document.querySelectorAll('video').forEach(video => {
                results.videos.push({
                    src: video.src,
                    currentSrc: video.currentSrc,
                    poster: video.poster,
                    sources: Array.from(video.querySelectorAll('source')).map(s => ({
                        src: s.src,
                        type: s.type
                    }))
                });
            });
            
            // Поиск React props
            const findReactProps = (element) => {
                for (const key in element) {
                    if (key.startsWith('__reactInternalInstance') || key.startsWith('__reactProps')) {
                        return element[key];
                    }
                }
                return null;
            };
            
            document.querySelectorAll('[class*="video"], [class*="media"]').forEach(el => {
                const props = findReactProps(el);
                if (props) {
                    results.reactProps.push(JSON.stringify(props).substring(0, 500));
                }
            });
            
            return results;
        }''')
        
        print(f"\nJavaScript analysis:")
        print(f"  Videos found: {len(result['videos'])}")
        for video in result['videos']:
            print(f"    src: {video.get('src')}")
            print(f"    currentSrc: {video.get('currentSrc')}")
    
    def print_network_analysis(self):
        print(f"\nTotal requests: {len(self.network_requests)}")
        print(f"Media URLs found: {len(self.media_urls)}")
        
        if self.media_urls:
            print("\nMedia URLs:")
            for media in self.media_urls:
                print(f"  {media['url']}")
                print(f"    Status: {media['status']}, Type: {media['content_type']}")
        
        # Анализируем CDN запросы
        cdn_requests = [r for r in self.network_requests if 'scontent' in r['url'] or 'cdninstagram' in r['url']]
        print(f"\nCDN requests: {len(cdn_requests)}")
        for req in cdn_requests[:10]:  # Первые 10
            print(f"  {req['url'][:100]}...")

async def main():
    analyzer = InstagramAnalyzer()
    url = "https://www.instagram.com/reel/DEWKjVHsYUb/"
    
    print("Starting Instagram page analysis...")
    results = await analyzer.analyze_page(url)
    
    # Сохраняем результаты
    with open('instagram_analysis_results.json', 'w', encoding='utf-8') as f:
        json.dump({
            'media_urls': results['media_urls'],
            'api_responses': [{'url': r['url'], 'body_preview': r['body'][:500]} for r in results['api_responses']]
        }, f, indent=2, ensure_ascii=False)
    
    print("\nAnalysis complete! Results saved to instagram_analysis_results.json")

if __name__ == "__main__":
    asyncio.run(main())