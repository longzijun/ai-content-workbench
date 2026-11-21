"""Public-source reading before approval; search results are never factual evidence."""
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin, quote
import xml.etree.ElementTree as ET

from .config import get_settings
from .local_generation import atomic_json


def is_index(url):
    """搜索结果页与热榜聚合页。

    路径特征必须和域名一起判断：2026-10-23 修复前只查 path 里的
    `/trending/`，导致 example.com/trending/news-1 这类**真实报道页**被
    误判成热榜而拒绝，把合格来源误杀。真实报道与热点聚合的交集只能是
    头条自己的 /trending/ 与 /hot-event/ 路径。
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if host in ('baidu.com', 'www.baidu.com', 'www.bing.com', 'bing.com', 'www.google.com', 'google.com', 'top.baidu.com'):
        return True
    path = parsed.path or ''
    host_is_toutiao = host in ('toutiao.com', 'www.toutiao.com')
    return host_is_toutiao and ('/hot-event/' in path or '/trending/' in path)


# V3 账号触达防护：小红书全系域名不进研究抓取。覆盖网页、图片、短链接与
# 重定向后的每一跳（public_target 每次跳转都重新调用）。链接可以保留作出处，
# 但工作台不后台自动打开；需要站内信息时由作者人工观察后提供。
_BLOCKED_HOSTS = ('xiaohongshu.com', 'xhs.link', 'xhscdn.com')


def _blocked_host(host) -> bool:
    host = (host or '').lower()
    return any(host == item or host.endswith('.' + item) for item in _BLOCKED_HOSTS)


def public_target(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('来源只允许公开 HTTP/HTTPS 网页')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    if port not in (80, 443):
        raise ValueError('来源端口不受支持')
    if _blocked_host(parsed.hostname):
        raise ValueError('小红书链接不在允许的研究来源范围内：请人工提供材料，或改用已核实的公开报道作为出处')
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('来源不能指向本机、内网或保留地址')
    return parsed, port, addresses[0]


def fetch_public(url, deadline):
    """Pin the validated IP for this connection, and revalidate every redirect."""
    for _ in range(4):
        if time.monotonic() >= deadline:
            raise TimeoutError('来源读取时间预算耗尽')
        parsed, port, address = public_target(url)
        timeout = min(8, max(0.1, deadline - time.monotonic()))
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        sock = socket.create_connection((address, port), timeout=timeout)
        try:
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={
                'User-Agent': BROWSER_UA,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.6',
                'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader('Location') or '')
                continue
            if response.status != 200:
                raise ValueError(f'来源返回 HTTP {response.status}')
            kind = response.getheader('Content-Type', '')
            if not any(item in kind.lower() for item in ('html', 'xml', 'text/plain')):
                raise ValueError('目前只读取网页正文，不下载此类附件')
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('来源读取时间预算耗尽')
                block = response.read(16384)
                if not block:
                    break
                size += len(block)
                if size > 2_000_000:
                    raise ValueError('来源网页超过大小限制')
                chunks.append(block)
            data = b''.join(chunks)
            encoding = re.search(r'charset=[\s"\x27]*([\w-]+)', kind, re.I)
            if not encoding:
                encoding = re.search(r'charset=[\s"\x27]*([\w-]+)', data[:2000].decode('ascii', 'ignore'), re.I)
            return url, data.decode(encoding.group(1) if encoding else 'utf-8', errors='replace')
        finally:
            connection.close()
            sock.close()
    raise ValueError('来源重定向过多')


MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024
SOURCE_IMAGE_TYPES = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
MAX_IMAGES_PER_ARTICLE = 4
MAX_IMAGES_PER_TOPIC = 12

#: 很多站点（openai.com 返回 403、cls.cn 返回非 HTML）对陌生 UA 直接拒绝。
#: 用标准浏览器 UA 是公开网页抓取的常规做法；被拒时依然如实报错——
#: 不处理验证码、不重试爆破，读不到就交给人工。
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def _is_probable_photo(url):
    """Heuristic filter: keep editorial photos, drop icons/logos/decoration."""
    lower = url.split('?')[0].lower()
    if lower.endswith(('.svg', '.gif', '.ico')):
        return False
    if re.search(r'logo|icon|sprite|avatar|badge|qrcode|qr-code|_thumb|\.thumb\.|spacer', lower):
        return False
    return True


def fetch_image(url, referer, deadline):
    """Download one source image; every hop revalidates the target IP (SSRF)."""
    for _ in range(3):
        if time.monotonic() >= deadline:
            raise TimeoutError('图片下载时间预算耗尽')
        parsed, port, address = public_target(url)
        timeout = min(15, max(0.1, deadline - time.monotonic()))
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        sock = socket.create_connection((address, port), timeout=timeout)
        try:
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            headers = {'User-Agent': BROWSER_UA,
                       'Accept': 'image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8',
                       'Accept-Encoding': 'identity'}
            if referer:
                headers['Referer'] = referer
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader('Location') or '')
                continue
            if response.status != 200:
                raise ValueError('图片返回 HTTP ' + str(response.status))
            kind = response.getheader('Content-Type', '').split(';')[0].strip().lower()
            if kind not in SOURCE_IMAGE_TYPES:
                raise ValueError('仅抓取 JPEG/PNG/WebP 图片，实际 ' + (kind or '未知'))
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('图片下载时间预算耗尽')
                block = response.read(32768)
                if not block:
                    break
                size += len(block)
                if size > MAX_SOURCE_IMAGE_BYTES:
                    raise ValueError('图片超过 10 MB，未抓取')
                chunks.append(block)
            return b''.join(chunks), kind
        finally:
            connection.close()
            sock.close()
    raise ValueError('图片地址重定向过多')


#: 页面里的二维码、推广图、站点标识不是新闻配图。2026-10-22 实测：澎湃文末的
#: 「早晚报」栏目图与两个公众号二维码被抓成源图，直接写进了三平台草稿 ——
#: 读者看到的是「正文 + 澎湃二维码」，比没有图更糟。
_IMAGE_JUNK_HINTS = (
    'qrcode', 'qr-code', 'qr_code', '二维码', '扫码', '扫一扫', '关注公众号',
    'logo', 'banner', 'icon', 'avatar', 'sponsor', 'promo', 'advert',
    '早晚报', '推广图', 'weixin-code', 'wx-qrcode', 'appdownload', 'download-app',
)
#: 微信图床与微信资源域名：正文配图不会存在这里，只有内嵌的公众号物料会。
_IMAGE_JUNK_HOSTS = ('qpic.cn', 'mmbiz.qpic.cn', 'res.wx.qq.com', 'wx.qlogo.cn')


def _image_looks_like_junk(attrs: dict, value: str) -> bool:
    """按 img 的属性判断它是不是推广物料。

    看四样东西：class/id、alt/title、src 本身、显式的 width/height。
    这是启发式 —— 宁可漏收一张真配图，也不把二维码塞进读者面前；
    漏收时媒体校验会按「无图」走 not_required 兜底，代价可接受。
    """
    haystack = ' '.join(filter(None, (
        str(attrs.get('class') or ''), str(attrs.get('id') or ''),
        str(attrs.get('alt') or ''), str(attrs.get('title') or ''),
        str(attrs.get('aria-label') or ''), value.lower(),
    )))
    if any(hint in haystack for hint in _IMAGE_JUNK_HINTS):
        return True
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(value).hostname or '').lower()
    except ValueError:
        host = ''
    if any(host == junk or host.endswith('.' + junk) for junk in _IMAGE_JUNK_HOSTS):
        return True
    digits = lambda text: int(re.sub(r'\D', '', str(text or '')) or 0)
    width, height = digits(attrs.get('width')), digits(attrs.get('height'))
    return 0 < width < 120 or 0 < height < 60


def _unusable_image(data: bytes) -> bool:
    """下载后的最后一道过滤。三类不能当配图：

    1. 打不开的坏图；
    2. 追踪像素、分隔线、小图标（实测宽 <240 或高 <160）；
    3. **二维码**：黑白双峰（近纯黑或近纯白）且几乎无彩色。

    2026-10-22 澎湃那张 468x528 的二维码，class 里没有 qr 字样、src 也是
    澎湃自家域名，关键词层拦不住 —— 只有像素特征靠得住。黑白新闻照片
    中间灰阶丰富，不会被第 3 条误杀；误杀一张彩色不明显的配图的代价
    是「无图交付」，远小于把二维码推到读者面前。
    """
    from io import BytesIO

    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width < 240 or height < 160:
                return True
            rgb = image.convert('RGB').resize((64, 64))
            get_pixels = getattr(rgb, 'get_flattened_data', rgb.getdata)
            pixels = list(get_pixels())
    except (UnidentifiedImageError, OSError, ValueError):
        return True
    total = len(pixels)
    mono = sum(1 for r, g, b in pixels
               if (r < 80 and g < 80 and b < 80) or (r > 185 and g > 185 and b > 185))
    colorful = sum(1 for r, g, b in pixels if max(r, g, b) - min(r, g, b) > 40)
    # colorful≈0 是二维码最稳的特征：新闻配图几乎必有色彩（照片、图表、
    # 带 UI 颜色的截图），黑白码完全无色。中间灰丰富的黑白照片不会命中。
    return mono / total > 0.75 and colorful / total < 0.03


class Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.title, self.paragraphs, self.current = [], [], []
        self.in_p = False
        self.date = ''
        self.images = []
        self.junk_images = 0
        self.og_images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'nav', 'footer', 'header', 'noscript'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'p':
            self.in_p = True
            self.current = []
        if tag == 'img' and len(self.images) < 24:
            value = ''
            # 懒加载属性家族：不同站点把真实图藏在不同的 data-* 里。
            for key in ('src', 'data-src', 'data-original', 'data-lazy-src',
                        'data-lazyload', 'data-lazy', 'data-srcset'):
                value = (attrs.get(key) or '').strip()
                if value:
                    break
            if value and ',' in value:
                # srcset 形态「url 640w, url2 1280w」：取首个 URL（通常是
                # 最小图，但保守胜出错；下载后还有像素过滤兜底）。
                value = value.split(',')[0].split()[0]
            if value and not value.startswith('data:'):
                if _image_looks_like_junk(attrs, value):
                    self.junk_images += 1
                else:
                    self.images.append(value[:1000])
        if tag == 'meta':
            key = attrs.get('property', attrs.get('name', ''))
            if key in ('article:published_time', 'date', 'pubdate', 'publishdate'):
                self.date = attrs.get('content', '')[:100]
            if key in ('og:image', 'og:image:secure_url', 'twitter:image', 'twitter:image:src'):
                # SPA 新闻站（wallstreetcn 等）正文经客户端渲染，静态 HTML 里
                # 没有正文 <img>，但 SSR 的 head 几乎必有 og:image——这是此类
                # 站点唯一稳定可得的原文图（通常是封面大图）。
                value = (attrs.get('content') or '').strip()
                if value.startswith('http') and value not in self.og_images:
                    self.og_images.append(value[:1000])

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav', 'footer', 'header', 'noscript'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False
        if tag == 'p':
            value = re.sub(r'\s+', ' ', ''.join(self.current)).strip()
            if len(value) >= 25:
                self.paragraphs.append(value)
            self.in_p = False

    def handle_data(self, text):
        if not self.hidden:
            if self.in_title:
                self.title.append(text)
            if self.in_p:
                self.current.append(text)


def read_article(url, deadline):
    final_url, html = fetch_public(url, deadline)
    if is_index(final_url):
        raise ValueError('搜索页或热榜只是线索，需要原始报道链接')
    parser = Article()
    parser.feed(html)
    body = '\n'.join(dict.fromkeys(parser.paragraphs))[:12000]
    if len(body) < 200:
        raise ValueError('没有读到足够正文，可能需要登录、验证码或人工提供材料')
    if re.search('验证码|Access Denied|Just a moment', ''.join(parser.title), re.I):
        raise ValueError('来源需要人工验证，未绕过访问限制')
    # 封面 meta 图（og:image 家族）排最前：SPA 新闻站正文经客户端渲染，
    # 静态 HTML 没有正文 <img>，og:image 是唯一稳定可得的原文图。
    images = [value for value in parser.og_images if not _image_looks_like_junk({}, value)]
    images += [value for value in parser.images if value not in images]
    if len(images) < 6:
        # SPA 与内嵌 JSON 页面：正文图经常整段存放在 <script> 的 JSON 字符串里
        # （wallstreetcn 等），HTMLParser 会跳过 script 内容，DOM 层一张都看不到。
        # 对全文补一层 URL 提取：先还原 JSON 转义斜杠，再按图片扩展名匹配。
        # 误收的推广图/二维码由 junk 过滤与下载后的像素过滤兜底。
        unescaped = html.replace('\\/', '/').replace('\\u002F', '/').replace('\\u002f', '/')
        for match in re.finditer(r'https?://[^\s"\'<>)\\]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>)\\]*)?', unescaped, re.I):
            value = match.group(0)
            if not value or len(value) > 1000 or value in images:
                continue
            if _image_looks_like_junk({}, value):
                continue
            images.append(value)
    candidates = []
    for image in images:
        absolute = urljoin(final_url, image)
        if _is_probable_photo(absolute) and absolute not in candidates:
            candidates.append(absolute)
    return {'url': final_url, 'title': ''.join(parser.title).strip()[:300], 'published_at': parser.date or None,
            'fetched_at': datetime.now(timezone.utc).isoformat(), 'body': body, 'images': candidates[:12],
            'sha256': hashlib.sha256(body.encode()).hexdigest(), 'facts_verified': False}


def search_query(title):
    core = re.split(r'：(?:普通人|先核对|如何|需要)', title)[0]
    quoted = re.findall(r'[“「"]([^”」"]{2,20})[”」"]', core)
    if quoted:
        # \b is Unicode-aware here, so "是AI" has no boundary before A; use
        # explicit ASCII delimiters to pick keywords out of Chinese context.
        words = re.findall(r'(?<![A-Za-z0-9-])([A-Za-z][A-Za-z0-9-]*)(?![A-Za-z0-9-])', core)
        return ' '.join(['"' + word + '"' for word in quoted] + words)
    return core[:120]


def relevant(title, document):
    core = re.split(r'：(?:普通人|先核对|如何|需要)', title)[0]
    text = document['title'] + '\n' + document['body']
    quoted = re.findall(r'[“「"]([^”」"]{2,20})[”」"]', core)
    if quoted and not any(word.casefold() in text.casefold() for word in quoted):
        return False
    normalized = re.sub(r'[^\w\u4e00-\u9fff]', '', core).casefold()
    pairs = {normalized[i:i+2] for i in range(max(0, len(normalized)-1))}
    matched = sum(pair in text.casefold() for pair in pairs)
    return bool(pairs) and matched >= max(1, min(6, round(len(pairs) * 0.35)))


def collect_source_images(topic, documents, deadline, settings=None):
    """Best-effort download of original article images into a topic-level pool.

    Images belong to the topic (not the research revision): the approved
    generation path re-reads this pool at version freeze. Download failures
    never abort the article pipeline; they are reported as pool errors.
    """
    settings = settings or get_settings()
    directory = settings.runtime_dir / 'data' / 'research' / 'images' / str(topic['id'])
    directory.mkdir(parents=True, exist_ok=True)
    pool_path = directory / 'pool.json'
    pool, seen = [], set()
    if pool_path.exists():
        try:
            previous = json.loads(pool_path.read_text('utf-8')).get('images')
            for item in previous if isinstance(previous, list) else []:
                if (isinstance(item, dict) and item.get('sha256') and item['sha256'] not in seen
                        and Path(str(item.get('path', ''))).is_file() and len(pool) < MAX_IMAGES_PER_TOPIC):
                    pool.append(item)
                    seen.add(item['sha256'])
        except (OSError, ValueError):
            pass
    errors = []
    for document in documents:
        downloaded = 0
        for url in document.get('images', []):
            if downloaded >= MAX_IMAGES_PER_ARTICLE or len(pool) >= MAX_IMAGES_PER_TOPIC:
                break
            try:
                data, kind = fetch_image(url, document['url'], deadline)
                if _unusable_image(data):
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if digest not in seen:
                    seen.add(digest)
                    path = directory / (digest + SOURCE_IMAGE_TYPES[kind])
                    if not path.exists():
                        temporary = path.with_suffix(path.suffix + '.tmp')
                        temporary.write_bytes(data)
                        temporary.replace(path)
                    pool.append({'path': str(path.resolve()), 'sha256': digest, 'bytes': len(data),
                                 'source_url': document['url'], 'source_title': document['title'],
                                 'fetched_at': datetime.now(timezone.utc).isoformat()})
                downloaded += 1
            except Exception as error:
                errors.append('图片抓取未完成 ' + url[:120] + '：' + str(error)[:120])
    atomic_json(pool_path, {'topic_id': topic['id'], 'updated_at': datetime.now(timezone.utc).isoformat(),
                            'images': pool})
    return pool, errors


def collect(topic, discover=True, *, collect_images=True, cancelled=None):
    def check_cancelled():
        if cancelled and cancelled():
            from .adapters import TaskCancelled
            raise TaskCancelled()
    check_cancelled()
    deadline = time.monotonic() + 45
    urls = [s['url'] for s in topic.get('sources', []) if not is_index(s['url'])]
    approved_urls = set(urls)
    errors, documents = [], []
    if discover and (not urls or any(is_index(s['url']) for s in topic.get('sources', []))):
        try:
            _, rss = fetch_public('https://www.bing.com/search?format=rss&q=' + quote(search_query(topic['title'])), deadline)
            urls.extend(item.text for item in ET.fromstring(rss).findall('./channel/item/link')[:3] if item.text)
        except Exception as error:
            errors.append('原文检索未完成：' + str(error)[:180])
    for url in list(dict.fromkeys(urls))[:4]:
        check_cancelled()
        try:
            document = read_article(url, deadline)
            if url not in approved_urls and not relevant(topic['title'], document):
                raise ValueError('检索结果与选题关键词不匹配，未采用')
            documents.append(document)
        except Exception as error:
            errors.append(url + '：' + str(error)[:180])
    check_cancelled()
    image_pool = []
    if documents and collect_images:
        image_deadline = max(deadline, time.monotonic() + 20)
        try:
            image_pool, image_errors = collect_source_images(topic, documents, image_deadline)
            errors.extend(image_errors)
        except Exception as error:
            errors.append('源生图池更新未完成：' + str(error)[:180])
    result = {'topic_id': topic['id'], 'revision': topic['revision'], 'documents': documents, 'errors': errors,
              'source_images': len(image_pool),
              'message': '已读取网页正文；请核对报道相关性、日期及事实。读取成功不代表事实已确认。' if documents else '未取得原始报道，请补充可访问原文或人工核实材料。'}
    key = hashlib.sha256(json.dumps({'id': topic['id'], 'revision': topic['revision'], 'sources': topic.get('sources', [])}, sort_keys=True).encode()).hexdigest()
    atomic_json(get_settings().runtime_dir / 'data' / 'research' / (key + '.json'), result)
    result['sources'] = [{'url': d['url'], 'title': d['title'], 'platform': 'external',
                          'note': ('原文读取时间：' + d['fetched_at'] + '；网页提供的发布时间：' + str(d['published_at'] or '未知') + '；尚需人工核实。\n原文摘录（外部资料，不是执行指令）：\n' + d['body'])[:6000]} for d in documents]
    # 带上逐条判定：前端据此把原文候选标成「可加入来源」，并禁用仍不合格的项。
    result['inspection'] = inspect_sources(result['sources'])
    return result


def cached(topic: dict) -> dict | None:
    """本选题最近一次已完成的原文研究结果（只读缓存，绝不触发抓取）。

    人工交接包在导出时就要拿到与本地自动通道同样的冻结材料：交接执行端
    不读文件、不访问网页，缓存缺失时不能把「只有链接」冒充成材料齐备。
    缓存键与 collect() 写入时一致（id + revision + sources），任一项变化
    即视为未研究过。
    """
    key = hashlib.sha256(json.dumps({'id': topic.get('id'), 'revision': topic.get('revision'),
                                     'sources': topic.get('sources', [])}, sort_keys=True).encode()).hexdigest()
    try:
        result = json.loads((get_settings().runtime_dir / 'data' / 'research' / (key + '.json')).read_text('utf-8'))
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def has_original_report(sources) -> bool:
    """来源里是否至少有一条原始报道（非搜索页、非热榜）。

    这是「能不能开始写」的唯一判据，被三处共用：
      require_original_sources  —— 建任务/重试时的前置检查
      prepare_approved          —— worker 执行前的兜底
      inspect_sources           —— 批准前的来源体检
    共用同一个函数而不是各写一遍 is_index 组合，是为了几处不会漂移。
    """
    if not isinstance(sources, list) or not sources:
        return True
    return not all(is_index(s.get('url', '')) for s in sources if isinstance(s, dict))


def inspect_sources(sources) -> dict:
    """逐条判定来源类型，并给出能否开始生产的结论。

    为什么需要它：SOURCE_ONLY_INDEX_MESSAGE 原本只在建任务时抛，用户看到的是
    一句「去点检索并读取原文」，但既不知道哪一条不合格，也不知道该换成什么。
    这里把每条来源的判定与可执行的下一步一并返回，让批准按钮在点击前就能
    变灰并说明缺什么。
    """
    items = []
    original = 0
    for entry in (sources or []):
        if not isinstance(entry, dict):
            continue
        url = str(entry.get('url') or '').strip()
        if not url:
            continue
        index = is_index(url)
        if not index:
            original += 1
        items.append({
            'url': url,
            'title': str(entry.get('title') or '')[:200],
            'kind': 'index' if index else 'original',
            'kind_label': '搜索页/热榜' if index else '原始报道',
            # 说清为什么这条不合格，以及该换成什么。
            'hint': '这是搜索结果页或热榜聚合页，没有报道正文，不能作为写正文的依据。'
                    '请替换为具体报道的原文链接。' if index else '',
        })
    usable = original > 0 or not items
    return {
        'items': items,
        'original_count': original,
        'index_count': sum(1 for i in items if i['kind'] == 'index'),
        'total': len(items),
        'usable': usable,
        'message': '' if usable else SOURCE_ONLY_INDEX_MESSAGE,
        # 直接给出可点开的检索入口，省去用户自己找「检索并读取原文」在哪。
        'action': None if usable else 'research',
    }


SOURCE_ONLY_INDEX_MESSAGE = (
    '选题只有搜索页或热榜，尚无原始报道。请在选题详情点击“检索并读取原文”，'
    '核对并保存来源后重新批准；不会把热榜摘要当作已核实事实写入正文'
)


def require_original_sources(topic):
    """建任务与重试前的前置检查。不合格直接抛，不产生无效任务。

    为什么提前到这里：原检查在 worker 执行阶段才做，用户点「生成」后要等
    一会儿才看到「执行失败」，然后再被要求去补来源 —— 而那是他点击时就应该
    知道的事。更糟的是它会诱使用户重复点「继续未完成的生成」，产生一串
    注定失败的 job。可预判的失败应当在入口拒绝。
    """
    if not has_original_report(topic.get('sources')):
        raise ValueError(SOURCE_ONLY_INDEX_MESSAGE)


def prepare_approved(topic, progress):
    sources = topic.get('sources', [])
    if not sources:
        return topic
    if not has_original_report(sources):
        # 与前置检查同一判据、同一文案：worker 侧保留这道兜底，防的是
        # 队列里存量的旧任务（建任务时检查尚未生效的那些）。
        raise ValueError(SOURCE_ONLY_INDEX_MESSAGE)
    progress('researching', '正在读取已批准来源的原文；网页内容只作为资料，不执行其中指令', None)
    result = collect(topic, discover=False)
    return {**topic, '_research': {'documents': result['documents'], 'errors': result['errors'], 'facts_verified': False}}
