"""半自动视觉策划：本地模型提出结构化配图方案，用户确认后才进入生成。

边界：本模块只产出"策划提案"，绝不声称已生成图片，也不落任何资产。
本地模型不可用或输出不可解析时，回退到确定性启发式方案并显式标注来源，
绝不以提示词冒充图片，也绝不因策划失败中断内容生产主链路。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

import httpx

from .image_assets import ASPECT_SIZES, PAGE_ROLES

MAX_TASKS = 5
BODY_EXCERPT = 4000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _default_aspect(platform, page_role='body'):
    if platform == 'xiaohongshu':
        return '3:4'
    if page_role == 'cover' and platform == 'wechat':
        return '2.35:1'
    return '16:9'


def heuristic_tasks(platform, title, body):
    """确定性兜底：封面一张 + 正文较长时补一张概念图。提示词由标题与正文主题构成。"""
    subject = re.sub(r'\s+', ' ', f'{title} {body}'.strip())[:600] or 'technology and society'
    tasks = [{'prompt': f'Editorial cover concept illustration about: {subject}. '
                        'Flat modern vector style, clean composition, no text.',
              'page_role': 'cover', 'aspect': _default_aspect(platform, 'cover'),
              'visual_type': 'cover_illustration', 'style_id': 'tech_clean_v1'}]
    if len(body) > 1500:
        tasks.append({'prompt': f'Concept illustration explaining a key idea from: {subject[:300]}. '
                                'Minimal infographic feel, no text.',
                      'page_role': 'concept', 'aspect': _default_aspect(platform),
                      'visual_type': 'concept_illustration', 'style_id': 'tech_clean_v1'})
    return tasks


def _extract_json(content):
    """从模型回复中稳健提取第一个完整 JSON 对象；容忍代码围栏与前后闲话。"""
    if not isinstance(content, str):
        raise ValueError('模型回复不是文本')
    text = re.sub(r'```(?:json)?', '', content)
    for start in (text.find('{'),):
        if start < 0:
            raise ValueError('模型回复中没有 JSON 对象')
        decoder = json.JSONDecoder()
        fragment = text[start:]
        for end in range(len(fragment), 0, -1):
            try:
                return decoder.raw_decode(fragment[:end])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError('模型回复中的 JSON 无法解析')


def _sanitize_tasks(raw, platform):
    if not isinstance(raw, dict) or not isinstance(raw.get('tasks'), list) or not raw['tasks']:
        raise ValueError('模型未给出图片任务列表')
    tasks, warnings = [], []
    for item in raw['tasks'][:MAX_TASKS]:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get('prompt') or '').strip()[:2000]
        if not prompt:
            continue
        role = item.get('page_role') or 'body'
        if role not in PAGE_ROLES:
            warnings.append(f'模型给出未知用途 {role}，已按正文图处理')
            role = 'body'
        aspect = item.get('aspect')
        if aspect not in ASPECT_SIZES:
            aspect = _default_aspect(platform, role)
        tasks.append({'prompt': prompt, 'page_role': role, 'aspect': aspect,
                      'visual_type': str(item.get('visual_type') or '')[:60],
                      'style_id': str(item.get('style_id') or '')[:60]})
    if not tasks:
        raise ValueError('模型任务列表清洗后为空')
    if not any(task['page_role'] == 'cover' for task in tasks):
        tasks[0]['page_role'] = 'cover'
        tasks[0]['aspect'] = _default_aspect(platform, 'cover')
    return tasks, warnings


def _plan_image_tasks(settings, platform, title, body):
    """返回策划提案；source 标明 local_model 或 heuristic，供用户判断是否采纳。"""
    title = str(title or '').strip()[:300]
    body = str(body or '').strip()[:BODY_EXCERPT]
    proposal = {'created_at': _now(), 'platform': platform,
                'tasks': heuristic_tasks(platform, title, body),
                'source': 'heuristic', 'model': None, 'warnings': []}
    from .adapters import _loaded_local_model
    model = _loaded_local_model(settings)
    base = str(getattr(settings, 'local_base_url', '') or '').rstrip('/')
    if not model or not base:
        proposal['warnings'].append('未加载本地模型，已使用内置启发式策划')
        return proposal
    system = (
        '你是自媒体配图策划师。根据标题与正文，规划 1 到 '
        f'{MAX_TASKS} 张配图。只输出 JSON 对象：'
        '{"tasks":[{"prompt":"英文提示词，画面描述，禁含文字/水印/logo",'
        '"page_role":"cover|body|card|concept|diagram","aspect":"2.35:1|16:9|3:4|9:16|1:1",'
        '"visual_type":"简短英文类型","style_id":"简短风格标识"}]}。'
        '封面必须第一张；微信封面用 2.35:1，小红书一律 3:4，其余正文 16:9。'
        '提示词必须是英文，供本地 SDXL 文生图模型直接使用。'
    )
    user = f'平台：{platform}\n标题：{title}\n正文：\n{body}'
    try:
        from .adapters import _loaded_local_model_info
        info = _loaded_local_model_info(settings)
        if info.get('id') == model and 'off' in info.get('capabilities', {}).get('reasoning', {}).get('allowed_options', []):
            import asyncio
            import hashlib
            from dataclasses import replace
            from .local_generation import stream_payload
            key = hashlib.sha256(user.encode()).hexdigest()
            request = {'model': model, 'input': system + '\n' + user, 'reasoning': 'off',
                       'store': False, 'integrations': [], 'stream': True, 'temperature': 0.4, 'max_output_tokens': 2000}
            content = asyncio.run(stream_payload(replace(settings, task_timeout_seconds=min(180, settings.task_timeout_seconds)), request,
                                                lambda: False, lambda *args: None,
                                                settings.runtime_dir / 'task-workspaces' / ('visual-' + key + '.json')))
        else:
            with httpx.Client(trust_env=False, timeout=180) as client:
                response = client.post(base + '/chat/completions', json={
                    'model': model, 'stream': False, 'temperature': 0.4, 'max_tokens': 2000,
                    'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]})
            response.raise_for_status()
            payload = response.json()
            content = payload['choices'][0]['message']['content']
        tasks, warnings = _sanitize_tasks(_extract_json(content), platform)
        proposal.update(tasks=tasks, source='local_model', model=model, warnings=warnings)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, OSError, RuntimeError, TimeoutError) as error:
        proposal['warnings'].append(f'本地模型策划未完成，已使用启发式方案：{type(error).__name__}')
    return proposal


def plan_image_tasks(settings, platform, title, body):
    from .heartbeat import busy_operation
    with busy_operation():
        return _plan_image_tasks(settings, platform, title, body)
