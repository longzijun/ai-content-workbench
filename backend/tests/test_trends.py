"""Trend persistence/approval boundaries; fixed simulated source responses only."""
from backend.tests.auth_helpers import bootstrap as authenticated_bootstrap
from datetime import datetime,timedelta,timezone
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import adapters,config,db,main,radar_platforms,radar_scoring,radar_trendradar,service,trend_sources,trends,worker


@pytest.fixture
def world(tmp_path,monkeypatch):
    runtime,vault=tmp_path/'runtime',tmp_path/'vault'
    vault.mkdir()
    monkeypatch.setenv('WORKBENCH_RUNTIME_DIR',str(runtime))
    monkeypatch.setenv('WORKBENCH_VAULT_PATH',str(vault))
    monkeypatch.setattr(config,'discover_codex',lambda:'')
    monkeypatch.setattr(worker,'start_worker',lambda:None)
    monkeypatch.setattr(worker,'stop_worker',lambda:None)
    monkeypatch.setattr(trends,'start_monitor',lambda:None)
    monkeypatch.setattr(trends,'stop_monitor',lambda:None)
    monkeypatch.setattr(adapters,'get_health',lambda s:{'cloud':{'enabled':False}})
    def forbidden(*a,**kw): pytest.fail('Trend test attempted actual network or model inference')
    monkeypatch.setattr(httpx,'get',forbidden)
    monkeypatch.setattr(httpx,'post',forbidden)
    monkeypatch.setattr(adapters,'generate_payload',forbidden)
    launches=[]
    monkeypatch.setattr(trends,'_launch_refresh',lambda rid,ids:launches.append((rid,ids)))
    db.init_db()
    return {'runtime':runtime,'patch':monkeypatch,'launches':launches}


def source_result(source_id='baidu_realtime',items=None,fetched=None,status='ok'):
    stamp=fetched or datetime.now(timezone.utc).isoformat()
    return {'source_id':source_id,'status':status,'fetched_at':stamp,'items':items if items is not None else [{'title':'新款机器人发布引发科技讨论','url':'https://www.baidu.com/s?wd=robot','summary':'公开热榜摘记仅用于线索，不代表事实已核实。','rank':1,'heat_value':1234567,'heat_label':'热搜指数','published_at':None}], 'error':None if status=='ok' else 'simulated source failed'}


def complete(world,result=None):
    run=trends.refresh()
    chosen=result or source_result()
    world['patch'].setattr(trend_sources,'collect_source',lambda sid:{**chosen,'source_id':sid})
    trends._refresh(run['run_id'],['baidu_realtime'])
    return trends.state()


def test_new_install_does_not_implicitly_fetch(world):
    assert trends.settings()['enabled'] is False
    assert trends.monitor_tick() is False
    assert world['launches']==[]
    assert trends.state()['refresh']['state']=='idle'


def test_repeated_refresh_during_active_run_returns_same_id(world):
    first=trends.refresh()
    second=trends.refresh()
    assert first['run_id']==second['run_id']
    assert len(world['launches'])==1 and second['state']=='running'
    assert service.state()['topics']==[] and service.state()['jobs']==[]


def test_source_item_dedup_preserves_real_history(world):
    first=complete(world)
    second=complete(world,source_result(fetched=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()))
    assert len(second['candidates'])==1 and first['candidates'][0]['id']==second['candidates'][0]['id']
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM trend_observations').fetchone()[0]==2
    assert len(service.state()['notifications'])==1


def test_same_title_distinct_publisher_retains_both_sources(world):
    current=trends.refresh()
    catalog={s['id']:s for s in trends._catalog()}
    a=source_result()
    b=source_result('bbc_technology',[{**a['items'][0],'url':'https://www.bbc.com/news/articles/technology-event','rank':None,'heat_value':None,'heat_label':None}])
    trends._record_source(current['run_id'],catalog['baidu_realtime'],a)
    trends._record_source(current['run_id'],catalog['bbc_technology'],b)
    data=trends.state()
    assert len(data['candidates'])==1
    assert len(data['candidates'][0]['sources'])==2
    with db.connect() as connection:
        assert {r['publisher_id'] for r in connection.execute('SELECT publisher_id FROM trend_observations')}=={'baidu','bbc'}


def test_multiple_google_feeds_have_one_publisher(world):
    catalog={s['id']:s for s in trends._catalog()}
    assert trends._publisher(catalog['google_ai'])==trends._publisher(catalog['google_research'])=='google'
    assert trends._publisher(catalog['bbc_world'])==trends._publisher(catalog['bbc_technology'])=='bbc'


@pytest.mark.parametrize('offset',[-9,1])
def test_old_or_future_publication_not_presented_as_fresh(world,offset):
    result=source_result()
    result['items'][0]['published_at']=(datetime.now(timezone.utc)+timedelta(days=offset)).isoformat()
    data=complete(world,result)
    assert data['candidates']==[]
    assert service.state()['notifications']==[]


def test_partial_failure_preserves_success_and_explicit_error(world):
    run=trends.refresh()
    world['patch'].setattr(trend_sources,'collect_source',lambda sid:source_result(sid,status='error') if sid=='bbc_world' else source_result(sid))
    trends._refresh(run['run_id'],['baidu_realtime','bbc_world'])
    data=trends.state()
    assert data['refresh']['state']=='partial'
    assert data['refresh']['source_counts']=={'ok':1,'error':1,'not_due':0,'pending':0,'attempted':2}
    assert '部分公开来源' in data['refresh']['error']
    assert next(s for s in data['sources'] if s['id']=='bbc_world')['status']=='error'
    assert len(data['candidates'])==1


def test_all_sources_fail_does_not_fake_candidates(world):
    data=complete(world,source_result(status='error',items=[]))
    assert data['refresh']['state']=='failed' and data['candidates']==[]
    assert data['sources'][0]['status']=='error'
    assert service.state()['topics']==[]


def test_trendradar_refresh_failure_never_falls_back_to_old_rows(world):
    run=trends.refresh()
    world['patch'].setattr(radar_trendradar,'refresh_output',lambda:{'status':'error','error':'本次采集失败'})
    world['patch'].setattr(radar_trendradar,'collect',lambda source_id:pytest.fail('失败后不得读取旧榜单'))
    trends._refresh(run['run_id'],['tr_toutiao'])
    data=trends.state()
    source=next(s for s in data['sources'] if s['id']=='tr_toutiao')
    assert data['refresh']['state']=='failed'
    assert source['status']=='error' and source['error']=='本次采集失败'


def test_not_due_preserves_persistent_failure_health(world):
    trends._record_health('bbc_world','error','持续超时')
    results=[{'source_id':'bbc_world','status':'not_due','fetched_at':None,'item_count':0,'error':None,'next_at':'2026-10-28T00:00:00+00:00'}]
    with db.transaction() as connection:
        connection.execute('INSERT INTO trend_runs(id,state,started_at,finished_at,source_results) VALUES(?,?,?,?,?)',
                           ('latest','succeeded',datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),json.dumps(results)))
    source=next(s for s in trends.state()['sources'] if s['id']=='bbc_world')
    # 未到采集间隔不等于来源不可用：status 只描述本轮调度结果（not_due），
    # 持续失败原因单独落在 health 里，界面据此分开展示，而不是把「没到点」
    # 误报成「这个源挂了」。
    assert source['status']=='not_due'
    assert source['health']['consecutive_failures']==1
    assert source['health']['last_error']=='持续超时'
    assert source['health']['degraded'] is False
    assert source['next_at']=='2026-10-28T00:00:00+00:00'


def test_direction_balanced_rows_keeps_ai_candidates_beyond_recency_cutoff(world):
    # 50 条社会热点（最近）+ 10 条 AI（更早）。旧的「先按时间截 N 条」会把
    # 时间靠后的 AI 整体丢掉；均衡取样应先按方向分桶，AI 方向即使时间靠后也保留。
    base = datetime.now(timezone.utc)
    rows = []
    for index in range(50):
        rows.append({'id': f'soc-{index}', 'title': f'社会事件 {index}', 'summary': '政策通报 民生 调查',
                     'directions': '["society"]', 'last_seen_at': (base - timedelta(minutes=index)).isoformat()})
    for index in range(10):
        rows.append({'id': f'ai-{index}', 'title': f'AI 模型发布 {index}', 'summary': '大模型 开源 发布',
                     'directions': '["ai"]', 'last_seen_at': (base - timedelta(days=1, minutes=index)).isoformat()})
    selected = trends._direction_balanced_rows(rows, limit=20)
    ai_ids = {row['id'] for row in selected if row['id'].startswith('ai-')}
    # AI 方向占比 50%，20 条配额给 AI 10 条 —— 全部 10 条 AI 候选都应入选，
    # 不会被更近的社会热点按时间排序挤出。
    assert ai_ids == {f'ai-{index}' for index in range(10)}
    assert len(selected) == 20


def test_candidates_use_six_dimension_score_for_default_order(world):
    original=radar_scoring.evaluate
    def scored(candidate,*args,**kwargs):
        result=original(candidate,*args,**kwargs)
        result['total']=95 if candidate['title'].startswith('官方') else 10
        return result
    world['patch'].setattr(radar_scoring,'evaluate',scored)
    world['patch'].setattr(radar_platforms,'recommend',lambda *args:{})
    run=trends.refresh()
    catalog={s['id']:s for s in trends._catalog()}
    hot=source_result(items=[{'title':'热榜事件','url':'https://www.baidu.com/s?wd=hot','summary':'热榜线索','rank':1,'heat_value':100,'heat_label':'指数','published_at':None}])
    official=source_result('bbc_world',items=[{'title':'官方科技发布','url':'https://www.bbc.com/news/articles/official-release','summary':'官方原始发布','rank':None,'heat_value':None,'heat_label':None,'published_at':datetime.now(timezone.utc).isoformat()}])
    trends._record_source(run['run_id'],catalog['baidu_realtime'],hot)
    trends._record_source(run['run_id'],catalog['bbc_world'],official)
    candidates=trends.state()['candidates']
    assert [item['title'] for item in candidates]==['官方科技发布','热榜事件']
    assert [item['score']['total'] for item in candidates]==[95,10]


def test_select_creates_pending_once_with_source_snapshot(world):
    candidate=complete(world)['candidates'][0]
    first=trends.select(candidate['id'],['toutiao'])
    second=trends.select(candidate['id'],['wechat'])
    assert first['topic']['id']==second['topic']['id']
    assert first['already_selected'] is False and second['already_selected'] is True
    assert first['topic']['status']=='pending' and first['topic']['platforms']==['toutiao']
    assert first['topic']['sources'][0]['url']==candidate['url']
    assert '核实' in first['topic']['sources'][0]['note']
    assert '来源未提供；抓取时间不等于发布时间' in first['topic']['sources'][0]['note']
    current=service.state()
    assert len(current['topics'])==1 and current['jobs']==[] and current['drafts']==[] and current['publications']==[]
    with pytest.raises(HTTPException):
        service.generate(first['topic']['id'],first['topic']['revision'])


def test_ignored_candidate_stays_ignored_on_next_scan(world):
    candidate=complete(world)['candidates'][0]
    trends.ignore(candidate['id'])
    assert complete(world)['candidates'][0]['status']=='ignored'
    assert service.state()['topics']==[]


def test_selected_source_snapshot_retains_publication_date(world):
    result=source_result()
    publication=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    result['items'][0]['published_at']=publication
    candidate=complete(world,result)['candidates'][0]
    chosen=trends.select(candidate['id'],['toutiao'])
    assert '候选记录的发布时间：'+publication in chosen['topic']['sources'][0]['note']
    assert '抓取时间：' not in chosen['topic']['sources'][0]['note']
    assert '证据状态：' in chosen['topic']['sources'][0]['note']
    assert chosen['topic']['status']=='pending' and service.state()['jobs']==[]


def test_selected_candidate_cannot_be_silently_ignored(world):
    candidate=complete(world)['candidates'][0]
    trends.select(candidate['id'],['toutiao'])
    with pytest.raises(HTTPException): trends.ignore(candidate['id'])


@pytest.mark.parametrize('value',[{'interval_minutes':1},{'interval_minutes':True},{'interval_minutes':1.5},{'scope':'everything'},{'enabled':'true'},{'enabled_sources':[]},{'enabled_sources':['http://127.0.0.1']},{'keywords':['a'*61]},{'window_days':31}])
def test_invalid_settings_do_not_authorize_monitoring(world,value):
    with pytest.raises(ValueError): trends.update_settings(value)
    assert trends.settings()['enabled'] is False


def test_settings_persist_and_normalize_keywords(world):
    desired=trends.update_settings({'enabled':True,'scope':'broad','keywords':[' 科技 ','科技'],'enabled_sources':['baidu_realtime'],'interval_minutes':60})
    assert desired['keywords']==['科技']
    assert trends.settings()==desired


def test_corrupt_settings_cannot_enable_network(world):
    path=trends._settings_path()
    path.write_text(json.dumps({'enabled':'false','enabled_sources':['evil']}),encoding='utf-8')
    assert trends.settings()['enabled'] is False
    assert trends.monitor_tick() is False


def test_overdue_schedule_does_one_scan_then_waits(world):
    trends.update_settings({'enabled':True,'interval_minutes':60})
    old=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    with db.transaction() as connection:
        connection.execute('INSERT INTO trend_runs(id,state,started_at,finished_at) VALUES(?,"succeeded",?,?)',('old-run',old,old))
    assert trends.monitor_tick() is True
    assert trends.monitor_tick() is False
    assert len(world['launches'])==1


def test_api_reads_require_session_and_writes_require_token(world):
    with TestClient(main.app) as client:
        assert client.get('/api/trends').status_code==401
        boot=authenticated_bootstrap(client).json()
        assert client.get('/api/trends').status_code==200
        assert client.post('/api/trends/refresh',json={}).status_code==403
        assert client.post('/api/trends/settings',json={'enabled':True}).status_code==403
        protected={'X-Workbench-Token':boot['token']}
        assert client.post('/api/trends/settings',json={'scope':'broad'},headers=protected).status_code==200
        assert client.post('/api/trends/refresh',json={},headers=protected).status_code==200


def test_api_select_never_confers_approval(world):
    candidate=complete(world)['candidates'][0]
    with TestClient(main.app) as client:
        headers={'X-Workbench-Token':authenticated_bootstrap(client).json()['token']}
        selected=client.post('/api/trends/'+candidate['id']+'/select',json={'platforms':['toutiao']},headers=headers)
        assert selected.status_code==200 and selected.json()['topic']['status']=='pending'
        assert service.state()['jobs']==[] and service.state()['drafts']==[]
