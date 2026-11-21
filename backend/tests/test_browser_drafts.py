"""Common browser adapter invariants; all UI actions are simulated."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.app import browser_drafts as adapter
from backend.app.browser_draft_state import BrowserDraftLedger
from backend.app.config import Settings


@pytest.fixture
def world(tmp_path, monkeypatch):
    work={'platform':'toutiao','format':'article','title':'核对内容与账号','body':'这一段是完整正文。','tags':[]}
    draft={'id':'draft-1','topic_id':'topic-1','version_id':'version-1','platform':'toutiao','state':'local','remote_id':None,'account_label':''}
    version={'id':'version-1','payload':{'works':[work]}}
    topic={'id':'topic-1'}
    settings=Settings(runtime_dir=tmp_path)
    events=[]
    controls={'read':True,'create_error':False,'prepare_error':None,'account':'测试账号','create_id':'1234567'}
    def prepare(page, content):
        events.append('prepare')
        if controls['prepare_error']:
            raise controls['prepare_error']
        return page
    def create(page, content, on_id):
        events.append('create')
        if controls['create_id']:
            on_id(controls['create_id'])
        if controls['create_error']:
            raise RuntimeError('simulated interrupted after save')
        return controls['create_id']
    def read(page, bare, content):
        events.append(('read',bare,deepcopy(content)))
        return controls['read']
    fake=SimpleNamespace(prepare=prepare,create=create,read=read)
    monkeypatch.setattr(adapter,'_module',lambda p:fake)
    monkeypatch.setattr(adapter.browser_bridge,'run_in_page',lambda p,callback,s,**kw: callback(object(),controls['account']))
    monkeypatch.setattr(adapter.browser_bridge,'mark_draft_verified',lambda *a:events.append('verified'))
    return SimpleNamespace(work=work,draft=draft,version=version,topic=topic,settings=settings,events=events,controls=controls,ledger=BrowserDraftLedger(tmp_path))


def run(w):
    return adapter.sync_browser_draft(w.draft,w.version,w.topic,w.settings)


def test_first_save_claims_once_before_writing(world):
    result=run(world)
    assert result['state']=='synced' and result['remote_id']=='browser:toutiao:1234567'
    assert world.ledger.load('topic-1','version-1','toutiao')['state']=='saved'
    assert world.events==['prepare','create','verified']
    second=run(world)
    assert second['state']=='synced'
    assert world.events.count('create')==1
    assert world.events[-2][0]=='read'


def test_xhs_is_local_browser_scope(world):
    world.work['platform']=world.draft['platform']='xiaohongshu'
    result=run(world)
    assert result['state']=='browser_saved' and result['destination']=='browser'
    assert result['remote_id'].startswith('browser:xiaohongshu:')


@pytest.mark.parametrize('known', [True,False])
def test_uncertain_write_is_never_created_again(world,known):
    world.controls.update(create_error=True,create_id='1234567' if known else None)
    first=run(world)
    assert first['state']=='unknown'
    assert bool(first['remote_id'])==known
    second=run(world)
    assert second['state']==('synced' if known else 'unknown')
    assert world.events.count('create')==1


def test_existing_db_id_without_ledger_allows_only_read(world):
    world.draft.update(remote_id='browser:toutiao:7654321',state='unknown')
    assert run(world)['state']=='synced'
    assert world.events[0][0]=='read' and 'create' not in world.events
    assert world.ledger.load('topic-1','version-1','toutiao') is None


def test_ledger_known_id_recovers_missing_db_result(world):
    claim=world.ledger.claim('topic-1','version-1','toutiao',world.work,'测试账号')
    world.ledger.record_saved(claim,'1234567')
    world.draft['state']='unknown'
    assert run(world)['remote_id']=='browser:toutiao:1234567'
    assert world.events[0][0]=='read' and 'create' not in world.events


def test_unknown_without_any_id_never_prepares(world):
    world.draft['state']='unknown'
    assert run(world)['state']=='unknown'
    assert world.events==[]


@pytest.mark.parametrize('change',['account','content'])
def test_bound_ledger_change_blocks_all_ui(world,change):
    world.ledger.claim('topic-1','version-1','toutiao',world.work,'测试账号')
    if change=='account': world.controls['account']='另一个账号'
    else: world.work['body']='变更后的正文'
    assert run(world)['state']=='blocked'
    assert world.events==[]


def test_existing_account_mismatch_blocks_read(world):
    world.draft.update(remote_id='browser:toutiao:1234567',account_label='原账号')
    result=run(world)
    assert result['state']=='unknown' and result['remote_id']==world.draft['remote_id']
    assert world.events==[]


def test_db_id_cannot_replace_ledger_id(world):
    claim=world.ledger.claim('topic-1','version-1','toutiao',world.work,'测试账号')
    world.ledger.record_saved(claim,'1234567')
    world.draft['remote_id']='browser:toutiao:9999999'
    assert run(world)['state']=='blocked'
    assert world.events==[]


def test_incompatible_provider_id_does_not_create(world):
    world.draft['remote_id']='official-api-media-id'
    assert run(world)['state']=='blocked'
    assert world.events==[]


def test_existing_content_mismatch_preserves_id(world):
    world.draft['remote_id']='browser:toutiao:1234567'
    world.controls['read']=False
    result=run(world)
    assert result['state']=='unknown' and result['remote_id']==world.draft['remote_id']
    assert 'create' not in world.events and 'verified' not in world.events


@pytest.mark.parametrize('exception,state',[(ValueError('too long'),'blocked'),(RuntimeError('changed UI'),'failed')])
def test_prepare_failure_has_no_ledger_or_write(world,exception,state):
    world.controls['prepare_error']=exception
    assert run(world)['state']==state
    assert world.events==['prepare']
    assert world.ledger.load('topic-1','version-1','toutiao') is None


def test_open_known_id_is_read_only_and_proved(world):
    world.draft['remote_id']='browser:toutiao:1234567'
    result=adapter.open_browser_draft(world.draft,world.version,world.topic,world.settings)
    assert result['state']=='opened' and result['content_verified'] is True and result['account_label']=='测试账号'
    assert result['remote_id']==world.draft['remote_id']
    assert world.events[0][0]=='read' and 'create' not in world.events


def test_open_missing_id_does_not_prepare(world):
    result=adapter.open_browser_draft(world.draft,world.version,world.topic,world.settings)
    assert result['state']=='blocked' and world.events==[]


def test_driver_timeout_retains_durable_id(world,monkeypatch):
    claim=world.ledger.claim('topic-1','version-1','toutiao',world.work,'测试账号')
    world.ledger.record_saved(claim,'1234567')
    monkeypatch.setattr(adapter.browser_bridge,'run_in_page',lambda *a,**kw:{'state':'unknown','error':'timed out'})
    assert run(world)['remote_id']=='browser:toutiao:1234567'
    assert world.events==[]


def test_corrupt_ledger_never_authorizes_create(world):
    world.ledger.claim('topic-1','version-1','toutiao',world.work,'测试账号')
    next(world.ledger.directory.glob('*.json')).write_text('broken',encoding='utf-8')
    with pytest.raises(RuntimeError):
        run(world)
    assert world.events==[]


def test_binding_mismatch_blocks_before_browser(world):
    world.topic['id']='other-topic'
    assert run(world)['state']=='blocked' and world.events==[]
