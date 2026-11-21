"""来源体检：批准生产前的预检。

真实背景（2026-10-23 赵老师反馈「每次批准生产时都会出现这个问题」）：
12 个选题里 8 个来源全是百度搜索页或头条热榜，点「批准并开始生产」会被
require_original_sources 拦下，但提示只说「去点检索并读取原文」——既没指出
是哪一条不合格，也没说下一步具体做什么。

这里锁死两件事：
1. inspect_sources 必须逐条点名，并给出可执行的下一步。
2. 它与 has_original_report 的结论必须一致 —— 两处判定不能漂移。
"""
import pytest

from backend.app.research import SOURCE_ONLY_INDEX_MESSAGE, has_original_report, inspect_sources, is_index


def src(url, title=''):
    return {'url': url, 'title': title, 'note': '', 'platform': 'external'}


SEARCH = src('https://www.baidu.com/s?wd=%E7%9F%AD%E5%89%A7', '百度搜索')
TRENDING = src('https://www.toutiao.com/trending/7697714955398726195/', '头条热榜')
REPORT = src('https://finance.sina.com.cn/tech/roll/2026-10-21/doc-inivyhks8018268.shtml', '新浪科技原文')
GXNEWS = src('https://www.gxnews.com.cn/staticpages/20261019/newgx6ad5e82e-22005905.shtml', '广西新闻网原文')


# --------------------------------------------------------------------------
# 与既有判据一致（防漂移）
# --------------------------------------------------------------------------

def test_inspection_agrees_with_the_gate():
    """两份结论必须一致：体检说不行，拦截就得拦。"""
    cases = [
        ([SEARCH], False), ([TRENDING], False),
        ([SEARCH, TRENDING], False), ([REPORT], True),
        ([SEARCH, REPORT], True), ([REPORT, GXNEWS], True),
        ([], True),
    ]
    for sources, expected in cases:
        assert inspect_sources(sources)['usable'] is expected, sources
        assert has_original_report(sources) is expected, sources


def test_gate_and_inspection_drift_detection():
    """构造一批随机组合，逐一对拍，任何漂移都会被抓到。"""
    pool = [SEARCH, TRENDING, REPORT, GXNEWS]
    for mask in range(1 << len(pool)):
        subset = [pool[i] for i in range(len(pool)) if mask & (1 << i)]
        assert inspect_sources(subset)['usable'] == has_original_report(subset), subset


# --------------------------------------------------------------------------
# 逐条点名
# --------------------------------------------------------------------------

def test_every_source_is_classified():
    result = inspect_sources([SEARCH, TRENDING, REPORT])
    kinds = [item['kind'] for item in result['items']]
    assert kinds == ['index', 'index', 'original']
    assert result['total'] == 3 and result['index_count'] == 2 and result['original_count'] == 1


def test_blocked_result_names_the_offending_sources():
    """这是本轮的核心：用户必须知道是哪一条挡住了批准。"""
    result = inspect_sources([SEARCH, TRENDING])
    assert result['usable'] is False
    assert result['message'] == SOURCE_ONLY_INDEX_MESSAGE
    assert result['action'] == 'research'
    bad = [item for item in result['items'] if item['kind'] == 'index']
    assert len(bad) == 2
    assert all(item['hint'] for item in bad), '每条不合格来源都要说明为什么、该换成什么'


def test_hint_explains_what_to_replace_it_with():
    result = inspect_sources([SEARCH])
    hint = result['items'][0]['hint']
    assert '原文' in hint and '不能作为' in hint


def test_original_sources_have_no_hint():
    result = inspect_sources([REPORT])
    assert result['items'][0]['hint'] == ''
    assert result['message'] == '' and result['action'] is None


def test_usable_result_reports_counts_for_display():
    result = inspect_sources([REPORT, SEARCH])
    assert result['usable'] is True
    assert result['original_count'] == 1 and result['index_count'] == 1
    assert result['action'] is None


# --------------------------------------------------------------------------
# 健壮性
# --------------------------------------------------------------------------

def test_empty_sources_are_usable():
    """没有来源时不算不合格 —— 由后续环节提示补充，不在这里拦死。"""
    result = inspect_sources([])
    assert result['usable'] is True and result['total'] == 0


def test_blank_urls_are_skipped():
    result = inspect_sources([src(''), src('   '), REPORT])
    assert result['total'] == 1 and result['items'][0]['url'] == REPORT['url']


def test_malformed_entries_do_not_crash():
    result = inspect_sources([None, 'string', 42, {'no_url': True}, REPORT])
    assert result['usable'] is True
    assert result['items'] == [{'url': REPORT['url'], 'title': '新浪科技原文',
                                'kind': 'original', 'kind_label': '原始报道', 'hint': ''}]


def test_none_sources_do_not_crash():
    assert inspect_sources(None)['usable'] is True


def test_titles_are_truncated():
    result = inspect_sources([src(REPORT['url'], '标' * 500)])
    assert len(result['items'][0]['title']) <= 200


def test_message_never_claims_reading_succeeded():
    """提示语不得暗示事实已核实。"""
    message = inspect_sources([SEARCH])['message']
    assert '不会把热榜摘要当作已核实事实写入正文' in message


# --------------------------------------------------------------------------
# is_index 边界（决定什么算索引页）
# --------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'https://www.baidu.com/s?wd=x',
    'https://baidu.com/s?wd=x',
    'https://top.baidu.com/board?tab=realtime',
    'https://www.bing.com/search?q=x',
    'https://www.google.com/search?q=x',
    'https://www.toutiao.com/trending/123/',
])
def test_known_index_pages(url):
    assert is_index(url) is True


@pytest.mark.parametrize('url', [
    'https://finance.sina.com.cn/tech/roll/2026-10-21/doc-x.shtml',
    'https://www.gxnews.com.cn/staticpages/20261019/newgx6ad5e82e.shtml',
    'https://news.china.com/socialgd/10000169/20261022/49809202.html',
    'https://www.163.com/dy/article/L9P3QLGJ0511U82T.html',
    # 路径里带 trending 但不在头条：不能误判
    'https://example.com/trending/news-1',
])
def test_real_report_pages(url):
    assert is_index(url) is False