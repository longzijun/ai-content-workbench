"""形式决策：推荐、人工采纳与持久化。

这一层存在的唯一理由，是让「选什么形式」真的影响生成结果。V2-04/03/05 已经把
规则引擎、AI 推荐器和注入点写完了，但决策从来没有被写进 `jobs.input_snapshot`
—— `local_generation` 里那句 `topic.get('_format_decision')` 永远拿到空值，
推荐器算得再准也传不到模型手上。

数据流：

    format_rules.evaluate(topic, platform)     规则引擎给出可行集合与排序
      ↓
    format_advisor.advise(...)                 可选：模型在可行集合内排序
      ↓
    topic_format_decisions (本表)              落库，人工可覆盖
      ↓
    service._create_job → input_snapshot       建任务时快照带过去
      ↓
    local_generation.generate_platforms        读 snapshot，注入 prompt 与 content_form

边界：
  不生成正文、不判断事实、不碰平台；
  没有决策时**不写空决策**——`_format_decision` 键必须「构造时不存在」，
  否则无决策路径的 prompt 会与改动前不同（有测试守这条）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import db, format_advisor, format_plan, format_rules, formats

PLATFORMS = ('wechat', 'toutiao', 'xiaohongshu')


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rule_topic(topic: dict) -> dict:
    """把 topics 表的行转成规则引擎要的形状。

    `sources` 是 `[{url,title,note}]`，规则引擎只数条数与标题，所以这里
    只抽 url —— 多给字段会让 inputs_digest 受无关变化影响。
    """
    try:
        sources = json.loads(topic.get('sources') or '[]')
    except (TypeError, ValueError):
        sources = []
    urls = [item.get('url') for item in sources
            if isinstance(item, dict) and isinstance(item.get('url'), str) and item['url'].strip()]
    return {
        'title': str(topic.get('title') or ''),
        'body': str(topic.get('brief') or ''),
        'kind': '',
        'source_urls': urls,
        'items': [],
    }


def _topic(topic_id: str) -> dict:
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if row is None:
        raise ValueError('选题不存在')
    return dict(row)


def evaluate_one(topic: dict, platform: str, now=None) -> dict:
    """单平台规则评估。纯函数外层，不写库。"""
    moment = now or datetime.now(timezone.utc)
    return format_rules.evaluate(platform=platform, topic=_rule_topic(topic),
                                 capabilities=None, now=moment)


def recommend(topic_id: str, platform: str, *, now=None, settings=None, ask_model=None) -> dict:
    """给出一个平台的推荐。默认走规则排序，`ask_model` 传入时才问模型。

    不传 settings/ask_model 时 status 是 `unavailable`，文案明写「按规则排序」——
    不能让用户以为模型参与了。规则排序本身已经是可行集合内的确定性结论。
    """
    if platform not in PLATFORMS:
        raise ValueError('不支持的发布平台')
    topic = _topic(topic_id)
    moment = now or datetime.now(timezone.utc)
    try:
        evaluation = format_rules.evaluate(platform=platform, topic=_rule_topic(topic),
                                           capabilities=None, now=moment)
    except ValueError as error:
        raise ValueError(f'形式评估失败：{error}')
    advice = format_advisor.advise(evaluation, _rule_topic(topic),
                                   settings=settings, ask_model=ask_model)
    return {
        'platform': platform,
        'evaluated_at': evaluation['evaluated_at'],
        'inputs_digest': evaluation['inputs_digest'],
        'recommended': advice.get('recommended') or '',
        'confidence': int(advice.get('confidence') or 0),
        'reason': advice.get('reason') or '',
        'alternatives': advice.get('alternatives') or [],
        'status': advice.get('status') or 'rule_only',
        'rule_ranked': advice.get('rule_ranked') or [],
        'notes': advice.get('note', ''),
        'evaluation_notes': evaluation.get('notes') or [],
        'feasible': evaluation['feasible'],
        'blocked': evaluation['blocked'],
        'catalog': {item['id']: item['label']
                    for item in formats.format_catalog()['axis_b']['items']},
    }


def recommend_all(topic_id: str, *, now=None, settings=None, ask_model=None) -> dict:
    """三个平台的推荐 + 已采纳的决策。前端一次拿全，少一轮往返。"""
    stored = {row['platform']: row for row in load(topic_id)}
    result = {}
    for platform in PLATFORMS:
        try:
            item = recommend(topic_id, platform, now=now, settings=settings, ask_model=ask_model)
        except ValueError as error:
            item = {'platform': platform, 'recommended': '', 'reason': str(error),
                    'status': 'failed', 'alternatives': [], 'rule_ranked': [],
                    'feasible': [], 'blocked': [], 'catalog': {}}
        saved = stored.get(platform)
        item['adopted'] = saved['adopted'] if saved else ''
        item['decided_by'] = saved['decided_by'] if saved else ''
        item['decided_at'] = saved['updated_at'] if saved else None
        item['stale'] = bool(saved and item.get('inputs_digest')
                             and saved['inputs_digest'] != item.get('inputs_digest'))
        result[platform] = item
    return {'topic_id': topic_id, 'generated_at': _stamp(), 'platforms': result}


def save(topic_id: str, platform: str, adopted: str, *, by: str = 'manual',
         recommendation: dict = None) -> dict:
    """保存人工采纳（或清空）。`adopted` 为空串表示撤销决策。"""
    if platform not in PLATFORMS:
        raise ValueError('不支持的发布平台')
    if not _topic(topic_id):
        raise ValueError('选题不存在')
    value = formats.normalize_content_form(adopted)
    if adopted and not value:
        raise ValueError('不支持的叙事形式')
    if by not in ('recommendation', 'manual'):
        raise ValueError('决策来源不合法')
    rec = recommendation or {}
    stamp = _stamp()
    with db.transaction() as connection:
        existing = connection.execute(
            "SELECT created_at FROM topic_format_decisions WHERE topic_id=? AND platform=?",
            (topic_id, platform)).fetchone()
        payload = (value, str(rec.get('recommended') or ''), int(rec.get('confidence') or 0),
                   str(rec.get('reason') or '')[:1000],
                   json.dumps(rec.get('alternatives') or [], ensure_ascii=False),
                   str(rec.get('status') or 'rule_only'),
                   json.dumps(rec.get('rule_ranked') or [], ensure_ascii=False),
                   str(rec.get('inputs_digest') or ''), by, stamp)
        if existing:
            connection.execute(
                "UPDATE topic_format_decisions SET adopted=?,recommended=?,confidence=?,reason=?,"
                "alternatives=?,status=?,rule_ranked=?,inputs_digest=?,decided_by=?,updated_at=? "
                "WHERE topic_id=? AND platform=?",
                (*payload[:9], stamp, topic_id, platform))
        else:
            connection.execute(
                "INSERT INTO topic_format_decisions(topic_id,platform,adopted,recommended,confidence,"
                "reason,alternatives,status,rule_ranked,inputs_digest,decided_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (topic_id, platform, *payload[:9], stamp, stamp))
    return {'topic_id': topic_id, 'platform': platform, 'adopted': value, 'decided_by': by,
            'updated_at': stamp}


def load(topic_id: str, connection=None) -> list:
    """读回已保存的决策。

    `connection` 可传入调用方已持有的事务连接 —— `service._create_job` 在
    `BEGIN IMMEDIATE` 里调本函数，另开一条连接在 Windows 上没必要。
    """
    sql = ("SELECT * FROM topic_format_decisions WHERE topic_id=? ORDER BY platform")
    if connection is not None:
        rows = connection.execute(sql, (topic_id,)).fetchall()
    else:
        with db.connect() as owned:
            rows = owned.execute(sql, (topic_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ('alternatives', 'rule_ranked'):
            try:
                item[key] = json.loads(item.get(key) or '[]')
            except (TypeError, ValueError):
                item[key] = []
        result.append(item)
    return result


def snapshot(topic_id: str, connection=None) -> dict:
    """建任务时塞进 `jobs.input_snapshot` 的形状。

    **没有采纳任何形式时返回空 dict**，调用方据此决定不加这个键。加一个
    空 `{'_format_decision': {'platforms': {}}}` 会让无决策路径的 prompt
    与改动前不同，破坏「不注入就不改变请求体」这条既有约定。
    """
    rows = [row for row in load(topic_id, connection) if row.get('adopted')]
    if not rows:
        return {}
    platforms = {}
    for row in rows:
        platforms[row['platform']] = {
            'adopted': row['adopted'],
            'recommended': row['recommended'],
            'decided_by': row['decided_by'],
            'decided_at': row['updated_at'],
        }
    return {'platforms': platforms}


def clear(topic_id: str, platform: str) -> dict:
    """撤销某平台的形式决策。"""
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM topic_format_decisions WHERE topic_id=? AND platform=?",
            (topic_id, platform))
    return {'topic_id': topic_id, 'platform': platform, 'adopted': ''}


#: 供测试与调用方确认键名与 format_plan 一致。
DECISION_KEY = format_plan.DECISION_KEY
