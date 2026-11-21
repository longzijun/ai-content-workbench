"""Constraints shared by generation, review and native draft adapters."""

from . import platform_registry

# Existing text layout/template consumers interpret TITLE_LIMITS as native
# article/note constraints. Video creative targets live in the registry and must
# not silently seed article layouts or be presented as official video limits.
TITLE_LIMITS = {platform: tuple(platform_registry.get(platform)["title_range"]) for platform in platform_registry.TEXT_PLATFORM_IDS}
NAMES = {platform: platform_registry.get(platform)["label"] for platform in platform_registry.PLATFORM_IDS}
NAMES["wechat"] = "公众号"  # Preserve the existing validation/error contract.


def expected_format(platform):
    """A 轴（渠道形态）在该平台的唯一允许值。本函数是 A 轴的权威来源。

    formats.axis_a_for 委托本函数，不复制这里的表达式。
    """
    if platform not in platform_registry.PLATFORM_SET:
        raise ValueError('不支持的发布平台')
    return platform_registry.expected_format(platform)


def work_errors(work):
    platform = work.get('platform')
    if platform not in platform_registry.PLATFORM_SET:
        return ['不支持的发布平台']
    if platform_registry.is_video_platform(platform) or work.get('format') == 'video_script':
        from . import video_script
        try:
            video_script.normalize_work(work)
        except (ValueError, TypeError) as error:
            return [str(error)]
        return []
    title, body = work.get('title'), work.get('body')
    low, high = TITLE_LIMITS[platform]
    errors = []
    if not isinstance(title, str) or not low <= len(title.strip()) <= high:
        errors.append(f'{NAMES[platform]}标题需为 {low} 到 {high} 字，请修改版本')
    if not isinstance(body, str) or not body.strip():
        errors.append(f'{NAMES[platform]}正文不能为空')
    if platform == 'xiaohongshu' and isinstance(body, str) and len(body) > 1000:
        errors.append('小红书正文超过 1000 字限制，请修改版本；不会自动截断')
    expected = expected_format(platform)
    if work.get('format') not in (None, expected):
        errors.append(f'{NAMES[platform]}当前仅支持 {expected} 草稿')
    return errors


def validate_work(work):
    errors = work_errors(work)
    if errors:
        raise ValueError('；'.join(errors))
