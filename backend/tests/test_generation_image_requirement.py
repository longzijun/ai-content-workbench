"""源生图优先策略（2026-10-20 用户决策）：AI 配图提示词降为可选。"""
import pytest
from backend.app.adapters import _schema,_validate_payload

@pytest.mark.parametrize('prompts',[[],['a'],['a','b','c']])
def test_optional_image_prompts_pass_through(prompts):
    payload={'strategy':'方法','works':[{'platform':'wechat','format':'article','title':'主题','body':'正文','tags':[],'image_prompts':prompts,'source_urls':[]}]}
    assert _validate_payload(payload,{'platforms':['wechat']})['works'][0]['image_prompts']==prompts

def test_missing_image_prompts_defaults_to_empty():
    payload={'strategy':'方法','works':[{'platform':'wechat','format':'article','title':'主题','body':'正文','tags':[],'source_urls':[]}]}
    assert _validate_payload(payload,{'platforms':['wechat']})['works'][0]['image_prompts']==[]

@pytest.mark.parametrize('prompts',[[''],['  '],['a','b','c','d']])
def test_excess_or_blank_prompts_still_fail(prompts):
    payload={'strategy':'方法','works':[{'platform':'wechat','format':'article','title':'主题','body':'正文','tags':[],'image_prompts':prompts,'source_urls':[]}]}
    with pytest.raises(ValueError,match='配图需求'):
        _validate_payload(payload,{'platforms':['wechat']})

def test_generated_schema_allows_empty_but_bounded_prompts():
    work_schema=_schema()['properties']['works']['items']
    assert 'image_prompts' not in work_schema['properties']
    assert 'image_prompts' not in work_schema['required']

def test_legacy_payload_with_image_prompts_is_tolerated():
    payload={'strategy':'方法','works':[{'platform':'wechat','format':'article','title':'主题','body':'正文','tags':[],'image_prompts':['旧版本字段'],'source_urls':[]}]}
    assert _validate_payload(payload,{'platforms':['wechat']})['works'][0]['image_prompts']==['旧版本字段']
