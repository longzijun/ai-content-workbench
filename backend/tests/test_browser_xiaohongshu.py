"""Local web-draft scope and native-save safety without a real browser."""
from types import SimpleNamespace

import pytest

from backend.app import browser_xiaohongshu as adapter


WORK={'platform':'xiaohongshu','format':'note','title':'审核后再发布','body':'核对完整的标题、正文与图片。','tags':[]}


@pytest.mark.parametrize('changes',[{'title':''},{'title':'字'*21},{'body':''},{'body':'字'*1001},{'format':'article'},{'platform':'toutiao'}])
def test_invalid_content_rejected_before_any_page_operation(changes):
    with pytest.raises(ValueError):
        adapter.prepare(object(),{**WORK,**changes})


def test_valid_limits_preserve_all_content():
    title,body=adapter._validate_work({**WORK,'title':'字'*20,'body':'字'*1000})
    assert len(title)==20 and len(body)==1000


class Host:
    def __init__(self,count=1,disabled='false',bounds=None):
        self.number=count
        self.disabled=disabled
        self.bounds=bounds or {'x':0,'y':0,'width':100,'height':100}
    def filter(self,**kw): return self
    def count(self): return self.number
    def get_attribute(self,name):
        assert name=='save-disabled'
        return self.disabled
    def bounding_box(self): return self.bounds


class Session:
    def __init__(self,nodes,quad=None):
        self.nodes=nodes
        self.quad=quad or [10,10,30,10,30,30,10,30]
        self.commands=[]
        self.detached=False
    def send(self,method,args=None):
        self.commands.append((method,args))
        if method=='Accessibility.getFullAXTree': return {'nodes':self.nodes}
        if method=='DOM.getBoxModel': return {'model':{'content':self.quad}}
        pytest.fail('Unexpected private API or browser mutation')
    def detach(self): self.detached=True


def node(label='暂存离开',identifier=2,**extra):
    return {'role':{'value':'button'},'name':{'value':label},'backendDOMNodeId':identifier,**extra}


def page(host=None,nodes=None,quad=None):
    clicks=[]
    session=Session(nodes if nodes is not None else [node('发布',1),node()],quad)
    control=host if host is not None else Host()
    waits=[]
    def wait_for_function(expression,**kwargs):
        assert 'xhs-publish-btn[is-save-draft="true"][save-text="暂存离开"]' in expression
        assert "getAttribute('save-disabled')==='false'" in expression and kwargs=={'timeout':30000}
        waits.append((control.count(),control.disabled))
        if getattr(control,'ready_after_wait',False):
            control.disabled='false'
        if control.count()!=1 or control.disabled!='false':
            raise TimeoutError('simulated save readiness deadline expired')
        if getattr(control,'disabled_after_wait',False):
            control.disabled='true'
    result=SimpleNamespace(locator=lambda selector:control,context=SimpleNamespace(new_cdp_session=lambda p:session),mouse=SimpleNamespace(click=lambda x,y:clicks.append((x,y))),wait_for_function=wait_for_function,waits=waits)
    return result,session,clicks


def test_native_click_targets_save_and_never_publish():
    target,session,clicks=page()
    adapter._save_leave(target)
    assert clicks==[(20,20)]
    assert session.commands[-1]==('DOM.getBoxModel',{'backendNodeId':2})
    assert session.detached


@pytest.mark.parametrize('nodes',[[node('发布',1)],[node(),node(identifier=3)],[node(ignored=True)],[node(properties=[{'name':'disabled','value':{'value':True}}])]])
def test_missing_ambiguous_or_disabled_save_never_clicks(nodes):
    target,session,clicks=page(nodes=nodes)
    with pytest.raises(adapter.XiaohongshuDraftUncertain):
        adapter._save_leave(target)
    assert clicks==[] and session.detached


@pytest.mark.parametrize('host',[Host(count=0),Host(count=2),Host(disabled='true')])
def test_host_cannot_authorize_save_before_ready(host):
    target,session,clicks=page(host=host)
    with pytest.raises((adapter.XiaohongshuDraftUncertain,TimeoutError)):
        adapter._save_leave(target)
    assert clicks==[] and session.commands==[]


def test_save_waits_until_delayed_editor_readiness_then_clicks_only_save():
    host=Host(disabled='true')
    host.ready_after_wait=True
    target,session,clicks=page(host=host)
    adapter._save_leave(target)
    assert target.waits==[(1,'true')] and clicks==[(20,20)] and session.detached


def test_save_rechecks_disabled_guard_after_wait_and_never_clicks_if_it_changes():
    host=Host(disabled='false')
    host.disabled_after_wait=True
    target,session,clicks=page(host=host)
    with pytest.raises(adapter.XiaohongshuDraftUncertain):
        adapter._save_leave(target)
    assert clicks==[] and session.commands==[]


def test_moved_accessible_button_is_not_clicked():
    target,session,clicks=page(quad=[200,200,220,200,220,220,200,220])
    with pytest.raises(adapter.XiaohongshuDraftUncertain):
        adapter._save_leave(target)
    assert clicks==[] and session.detached


@pytest.mark.parametrize('identifier',['','api-note-id','browser:xiaohongshu:fake','827e33f7-2fdb-4028-ad4b-2dc4460002d2" onclick="publish'])
def test_invalid_native_id_never_opens_a_page(identifier):
    assert adapter.read(object(),identifier,WORK) is False
