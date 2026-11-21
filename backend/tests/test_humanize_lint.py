"""C0 deterministic style warnings vs hard format rules."""
from backend.app import humanize_lint


def test_concrete_plain_article_has_no_findings():
    body = "打开工具的导入菜单，选择刚才保存的文件。\n\n我核对了输出结果，三次都显示同一条记录。"
    assert humanize_lint.check(body, "article") == ([], [])


def test_firstly_secondly_is_warning_not_hard_error():
    violations, advisories = humanize_lint.check("首先在工具里选择导入，其次核对输出结果。", "article")
    assert violations == []
    assert any("首先" in item and "真实逻辑可保留" in item for item in advisories)


def test_parallel_connectives_are_advisories_only():
    violations, advisories = humanize_lint.check("这个工具不仅能导入文件，还能核对输出结果。", "article")
    assert violations == []
    assert any("机械连接词" in item for item in advisories)


def test_abstract_words_are_not_a_vocabulary_ban():
    violations, advisories = humanize_lint.check("这个工具的闭环设置会改变导出结果。", "article")
    assert violations == []
    assert any("空泛抽象词" in item for item in advisories)


def test_repeated_paragraph_summaries_are_warning():
    body = "打开工具菜单。所以，先配置导入。\n\n选择文件。总之，要核对名称。\n\n查看结果。可见，输出能复查。"
    violations, advisories = humanize_lint.check(body, "article")
    assert violations == []
    assert any("多个段落以总结句收尾" in item for item in advisories)


def test_empty_emotion_words_are_warning():
    violations, advisories = humanize_lint.check("这个强大、领先、极致的工具能导出结果。", "article")
    assert violations == []
    assert any("空泛情绪词" in item for item in advisories)


def test_missing_tool_action_and_result_have_separate_warnings():
    violations, advisories = humanize_lint.check("这件事值得认真讨论，也应该形成清楚认识。", "article")
    assert violations == []
    assert any("缺少具体工具、操作、截图或原始输出" in item for item in advisories)
    assert any("缺少真实结果、数字或可对照的前后变化" in item for item in advisories)


def test_normal_tool_name_number_and_quote_are_not_killed():
    body = "在 Obsidian 中打开设置，输入“模板”，选择第 2 项。\n\n输出结果显示 3 条记录，和修改前一致。"
    assert humanize_lint.check(body, "article") == ([], [])


def test_last_step_is_not_a_mechanical_summary():
    assert humanize_lint.check("最后一步是在工具里核对导出结果。", "article") == ([], [])


def test_xiaohongshu_markup_is_hard_format_error():
    violations, _ = humanize_lint.check("## 工具设置\n\n**输出结果**", "note")
    assert violations


def test_article_markup_is_allowed():
    assert humanize_lint.check("## 工具设置\n\n**输出结果**显示 2 条记录。", "article") == ([], [])


def test_note_emoji_is_advisory_not_hard_error():
    violations, advisories = humanize_lint.check("工具导入 ✅✨🔥📌✅，输出结果有 2 条。", "note")
    assert violations == []
    assert any("emoji" in item for item in advisories)


def test_prompt_leak_is_hard_error_but_prompt_tool_discussion_is_allowed():
    violations, _ = humanize_lint.check("材料边界：请只根据选题输出正文。", "article")
    assert any("复述了执行提示词" in item for item in violations)
    body = "在工具里打开提示词设置，输入两条规则。\n\n输出结果从 3 条变成 1 条。"
    assert humanize_lint.check(body, "article") == ([], [])


def test_article_format_and_internal_tracking_remain_hard_errors():
    violations, _ = humanize_lint.check("[工具结果](https://example.invalid)", "article")
    assert any("链接或图片语法" in item for item in violations)
    violations, _ = humanize_lint.check("TrendRadar 数据显示，相关记录出现过 3 次", "article")
    assert any("内部采集记录" in item for item in violations)


def test_ip08_cliches_are_hard_errors_but_quotes_and_sources_are_exempt():
    for phrase in ("在当今AI时代", "值得注意的是", "总而言之", "让我们一起"):
        violations, _ = humanize_lint.check(phrase + "，打开工具核对输出结果。", "article")
        assert any("套话" in item for item in violations), phrase
    body = "受访者的原话是“总而言之，先核对结果”。\n\n来源：《在当今AI时代怎样使用工具》\n\n打开工具设置，输出结果有 2 条。"
    violations, _ = humanize_lint.check(body, "article")
    assert violations == []


def test_empty_input_is_safe():
    assert humanize_lint.check("", "article") == ([], [])
    assert humanize_lint.check(None, "article") == ([], [])
