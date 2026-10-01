"""Private subprocess fixtures only: no provider, service, or owner credentials."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from app import agent_write_tools as tools
from app.app_tools import AppTool
from app.events import process_event

WRITE={"id":"operation1","run_id":"run1","tool":"checkpoint_chat","arguments":{"summary":"synthetic"}}


def assert_activity_settled(events, exit_code):
  assert [e['type'] for e in events]==['tool_start','tool_output','tool_end']
  assert isinstance(events[0]['input'], str)
  assert json.loads(events[0]['input']) == WRITE['arguments']
  assert len({e['tool_use_id'] for e in events})==1
  blocks=[]
  for event in events:
    assert process_event(event,blocks)
  assert len(blocks)==1
  assert blocks[0]['status']=='done'
  assert blocks[0]['delivery']=='quiet'
  assert blocks[0]['output_exit_code']==exit_code
  return events[1]


def test_only_reviewed_result_independent_tools_use_quiet_delivery():
  apps=[AppTool(1,"memory_remember","remember","Capture",{},result_independent=True),
        AppTool(2,"reflection_log_friction","log_friction","Record friction",{},result_independent=True),
        AppTool(1,"memory_search","search","Find",{})]
  for top_level in (True,False):
    names=tools.eligible_tool_names(app_tools=apps,top_level=top_level)
    assert names==frozenset({"checkpoint_chat","memory_remember","reflection_log_friction"})


def dispatcher(tmp_path,monkeypatch,body,events):
  script=tmp_path/"fake_control.py"
  script.write_text("import sys,json,os,time\nr=json.load(sys.stdin)\n"+body)
  monkeypatch.setattr(tools,"_control_script",lambda:str(script))
  return tools.QuietToolDispatcher(env={"AGENT_TOKEN":"synthetic-secret","CHAT_ID":"exact-child"},
    eligible_tools=frozenset({"checkpoint_chat"}),publish=events.append)


def test_existing_worker_receipt_reaches_ui_but_not_a_provider(tmp_path,monkeypatch):
  events=[]
  call=dispatcher(tmp_path,monkeypatch,'''
assert os.environ['CHAT_ID']=='exact-child'
assert r['params']['arguments']=={'summary':'synthetic'}
assert r['params']['_meta']=={'mobius/writeOperationId':'operation1'}
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'content':[{'type':'text','text':'Saved.'}],'isError':False}}))
''',events)
  outcome=asyncio.run(call(WRITE))
  assert outcome.status=='succeeded'
  assert 'Saved.' in assert_activity_settled(events,0)['content']


def test_failure_is_bounded_and_run_secret_redacted(tmp_path,monkeypatch):
  events=[]
  call=dispatcher(tmp_path,monkeypatch,'''
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'content':[{'type':'text','text':os.environ['AGENT_TOKEN']+' denied '+'x'*1000}],'isError':True}}))
''',events)
  outcome=asyncio.run(call(WRITE))
  assert outcome.status=='failed' and len(outcome.reason)==500
  assert 'synthetic-secret' not in str(events)+outcome.reason
  assert_activity_settled(events,1)


@pytest.mark.parametrize('body',[
  "print('not json')", "sys.exit(1)",
  "print(json.dumps({'jsonrpc':'2.0','id':'wrong','result':{}}))",
  "print('x'*2000000)",
])
def test_worker_ambiguity_is_unknown_never_a_retry_or_success(tmp_path,monkeypatch,body):
  events=[];call=dispatcher(tmp_path,monkeypatch,body,events)
  assert asyncio.run(call(WRITE)).status=='unknown'
  assert_activity_settled(events,1)


def test_quiet_payload_cannot_override_host_identity_or_select_result_tools(tmp_path,monkeypatch):
  events=[];call=dispatcher(tmp_path,monkeypatch,"raise AssertionError('must not execute')",events)
  for write in ({**WRITE,"tool":"request_restart"},
                {**WRITE,"arguments":{"_mobius_caller_env_file":"/different/helper"}}):
    assert asyncio.run(call(write)).status=='failed'
  assert not events


def test_worker_has_materialized_identity_not_shared_host_startup_indirection(tmp_path,monkeypatch):
  events=[]
  call=dispatcher(tmp_path,monkeypatch,'''
assert 'MOBIUS_CALLER_ENV_FILE' not in os.environ
assert 'MOBIUS_HELPER_HOST' not in os.environ
assert os.environ['CHAT_ID']=='exact-child'
print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':{'content':[],'isError':False}}))
''',events)
  call=tools.QuietToolDispatcher(env={**call.env,'MOBIUS_CALLER_ENV_FILE':'/synthetic-not-opened',
    'MOBIUS_HELPER_HOST':'shared'},eligible_tools=call.eligible_tools,publish=events.append)
  assert asyncio.run(call(WRITE)).status=='succeeded'


def test_fast_exited_leader_cannot_leave_descendant_holding_cleanup_open(tmp_path,monkeypatch):
  events=[];pidfile=tmp_path/'descendant'
  call=dispatcher(tmp_path,monkeypatch,f'''
pid=os.fork()
if pid:
  open({str(pidfile)!r},'w').write(str(pid))
  os._exit(0)
time.sleep(60)
''',events)
  monkeypatch.setattr(tools,'CONTROL_TOOL_TIMEOUT_SECONDS',0.2)
  async def run():
    return await asyncio.wait_for(call(WRITE),5)
  assert asyncio.run(run()).status=='unknown'
  pid=int(pidfile.read_text())
  stat=Path(f'/proc/{pid}/stat')
  assert not stat.exists() or stat.read_text().rsplit(')',1)[1].split()[0]=='Z'
  assert_activity_settled(events,1)


@pytest.mark.parametrize('cancel',[False,True])
def test_timeout_or_cancellation_joins_the_exact_worker(tmp_path,monkeypatch,cancel):
  events=[];pidfile=tmp_path/'pid'
  call=dispatcher(tmp_path,monkeypatch,f"open({str(pidfile)!r},'w').write(str(os.getpid()))\ntime.sleep(60)",events)
  async def run():
    if not cancel:monkeypatch.setattr(tools,'CONTROL_TOOL_TIMEOUT_SECONDS',0.2)
    task=asyncio.create_task(call(WRITE))
    if cancel:
      async with asyncio.timeout(5):
        while not pidfile.exists():await asyncio.sleep(0.01)
      task.cancel()
      with pytest.raises(asyncio.CancelledError):await task
    else:assert (await task).status=='unknown'
  asyncio.run(run())
  assert not Path('/proc',pidfile.read_text()).exists()
  assert_activity_settled(events,1)
