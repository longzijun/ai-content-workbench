"""冷启动（LM Studio 按需加载模型）的可恢复性测试。

背景：切换模型档位后的首个请求会先走 model_load，这段时间 SSE 流可能提前结束、
收不到 chat.end。原实现把它判成"本地响应未确认正常结束"，用户完全看不出是在加载模型。
这里锁定四条：加载过程要报给用户、加载期中断要能重试、重试一次后仍失败要如实报错、
已经加载完成的空流不能被误判成冷启动。
"""
import asyncio
import json

import httpx
import pytest

from backend.app import adapters, config, db, delivery, local_generation


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH', str(tmp_path / 'vault'))
    monkeypatch.setattr(config, 'discover_codex', lambda: '')
    monkeypatch.setattr(adapters, '_loaded_local_model_info', lambda _: {})
    delivery._stop.clear()
    db.init_db()
    return config.get_settings()


def mock_streams(monkeypatch, *streams):
    """按请求顺序依次返回预置的 SSE 流；最后一条会被重复使用。"""
    original = httpx.AsyncClient
    queue = list(streams)

    def handler(request):
        events = queue.pop(0) if len(queue) > 1 else queue[0]
        data = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
        return httpx.Response(200, content=data)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def native_request():
    return {'model': 'test', 'input': '写一篇稿子', 'max_output_tokens': 100}


COLD_START_STREAM = [{'type': 'chat.start'}, {'type': 'model_load.start'}]

HAPPY_STREAM = [
    {'type': 'chat.start'},
    {'type': 'model_load.start'},
    {'type': 'model_load.progress', 'progress': 0.4},
    {'type': 'model_load.progress', 'progress': 0.9},
    {'type': 'model_load.end', 'load_time_seconds': 11.96},
    {'type': 'message.delta', 'content': '{"strategy":"x"}'},
    {'type': 'chat.end', 'result': {'stats': {'total_output_tokens': 6}, 'output': [{'type': 'message'}]}},
]


def collect_progress():
    messages = []

    def progress(stage, message, provider=None):
        messages.append(message)

    return messages, progress


def test_model_load_is_reported_instead_of_silent(world, monkeypatch):
    mock_streams(monkeypatch, HAPPY_STREAM)
    messages, progress = collect_progress()
    result = asyncio.run(local_generation.stream_payload(world, native_request(), lambda: False, progress, world.runtime_dir / 'diag.json'))
    assert result == '{"strategy":"x"}'
    joined = ' | '.join(messages)
    assert '正在加载' in joined
    assert '40%' in joined and '90%' in joined
    assert '已加载完成' in joined


def test_loading_interrupted_stream_raises_retryable_signal(world, monkeypatch):
    mock_streams(monkeypatch, COLD_START_STREAM)
    _, progress = collect_progress()
    with pytest.raises(local_generation.LocalColdStart):
        asyncio.run(local_generation.stream_payload(world, native_request(), lambda: False, progress, world.runtime_dir / 'diag.json'))


def test_retry_once_recovers_after_cold_start(world, monkeypatch):
    mock_streams(monkeypatch, COLD_START_STREAM, HAPPY_STREAM)
    messages, progress = collect_progress()
    result = local_generation.stream_payload_with_retry(world, native_request(), lambda: False, progress, world.runtime_dir / 'wechat.partial.json')
    assert result == '{"strategy":"x"}'
    assert any('自动重试一次' in message for message in messages)
    assert (world.runtime_dir / 'wechat.partial.retry.json').is_file(), '重试的诊断记录要单独留档，不能覆盖第一次'


def test_second_cold_start_reports_honestly(world, monkeypatch):
    mock_streams(monkeypatch, COLD_START_STREAM, COLD_START_STREAM)
    _, progress = collect_progress()
    with pytest.raises(ValueError, match='仍未返回完整响应'):
        local_generation.stream_payload_with_retry(world, native_request(), lambda: False, progress, world.runtime_dir / 'diag.json')


def test_loaded_model_ending_early_is_not_treated_as_cold_start(world, monkeypatch):
    """模型已加载完成却仍无正文：这是真问题，不能靠重试掩盖。"""
    completed_load = [{'type': 'chat.start'}, {'type': 'model_load.start'},
                      {'type': 'model_load.end', 'load_time_seconds': 8}]
    mock_streams(monkeypatch, completed_load)
    _, progress = collect_progress()
    with pytest.raises(ValueError, match='未确认正常结束'):
        asyncio.run(local_generation.stream_payload(world, native_request(), lambda: False, progress, world.runtime_dir / 'diag.json'))


def test_openai_style_empty_stream_keeps_original_error(world, monkeypatch):
    """非 native（/chat/completions）路径没有 model_load 事件，必须保持原有失败语义。"""
    mock_streams(monkeypatch, [{'choices': [{'delta': {}, 'finish_reason': None}]}])
    _, progress = collect_progress()
    with pytest.raises(ValueError, match='未确认正常结束'):
        local_generation.stream_payload_with_retry(world, {'model': 'test'}, lambda: False, progress, world.runtime_dir / 'diag.json')
