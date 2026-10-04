"""Helper verdicts are distinct from narration; transcripts remain full evidence."""
import asyncio
from types import SimpleNamespace

import pytest

from app import models
from app.broadcast import ChatBroadcast
from app.chat_event_sink import ChatEventSink
from app.chat_writer import FinishRun, StartTurn, get_writer
from app.delegations import _assistant_result, _compose_wake_notice
from tests.test_helper_hosts import _claude_host, _turn

TOKEN = 'helper-result-test'


def sink_for(chat):
  get_writer().submit(StartTurn(chat_id=chat.id, run_token=TOKEN,
    user_msg={'role': 'user', 'content': 'Test', 'ts': 10},
    title_source='Test')).result(timeout=5)
  return ChatEventSink(ChatBroadcast(chat.id), chat.id, run_token=TOKEN)


@pytest.mark.parametrize('source', ['content', 'reference'])
@pytest.mark.parametrize('terminal_status', ['completed', 'failed', 'stopped'])
def test_result_is_persisted_without_replaying_prose(chat, db, source, terminal_status):
  async def scenario():
    sink = sink_for(chat)
    narration = 'Looking at another source. ' * 300
    sink.publish({'type': 'text_final', 'text_item_id': 'progress', 'content': narration})
    report = 'Verdict: fix the delivery boundary.'
    sink.publish({'type': 'text_final', 'text_item_id': 'answer', 'content': report})
    event = {'type': 'assistant_result', **(
      {'content': report} if source == 'content' else {'text_item_id': 'answer'})}
    sink.publish(event)
    sink.publish(event)
    await sink.finalize()
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    message = saved.messages[-1]
    assert message['result'] == report
    assert narration in message['content']
    assert message['content'].count(report) == 1
    assert _assistant_result(saved) == report
    get_writer().submit(FinishRun(chat_id=chat.id, run_token=TOKEN,
                                 terminal_status=terminal_status)).result(timeout=5)
    row = models.Delegation(id='result-helper', parent_chat_id='parent',
                            child_chat_id=chat.id, task_key='inspect')
    notice = _compose_wake_notice(db, [row], {row.id: TOKEN})
    assert report in notice and 'Looking at another' not in notice
    assert f'"status":"{terminal_status}"' in notice
    assert '"result_truncated":false' in notice
  asyncio.run(scenario())


def test_result_never_bleeds_into_a_new_steered_segment(chat):
  sink = sink_for(chat)
  sink.publish({'type': 'assistant_result', 'content': 'Old verdict'})
  sink.assistant_message_id = f'{TOKEN}:assistant:1'
  sink.assistant_blocks = [{'type': 'text', 'content': 'New work'}]
  snapshot, _ = sink._deferred_snapshot(sink.assistant_blocks)
  assert 'result' not in snapshot


def test_legacy_result_uses_latest_text_not_joined_narration():
  chat = SimpleNamespace(messages=[{'role': 'assistant', 'content': 'old narration + report',
    'blocks': [{'type': 'text', 'content': 'old narration'},
               {'type': 'text', 'content': 'report'},
               {'type': 'error', 'message': 'Provider stopped'}]}])
  assert _assistant_result(chat) == 'report\n\nProvider stopped'
  chat.messages = [{'role': 'assistant', 'content': 'Legacy content-only report'}]
  assert _assistant_result(chat) == 'Legacy content-only report'


def test_explicit_result_keeps_a_later_failure_actionable():
  chat = SimpleNamespace(messages=[{'role': 'assistant',
    'result': 'Substantive findings', 'blocks': [
      {'type': 'text', 'content': 'Narration'},
      {'type': 'error', 'message': 'DELEGATION_WRITE_REVIEW_REQUIRED: Inspect before retry'},
    ]}])
  assert _assistant_result(chat) == (
    'Substantive findings\n\nDELEGATION_WRITE_REVIEW_REQUIRED: Inspect before retry')


def test_report_only_terminal_does_not_hide_recorded_failure(chat, db):
  async def scenario():
    sink = sink_for(chat)
    sink.publish({'type': 'assistant_result', 'content': 'Useful report'})
    sink._last_error = 'Transport ended unexpectedly'
    await sink.finalize()
    db.expire_all()
    saved = db.get(models.Chat, chat.id)
    assert saved.messages[-1]['blocks'] == [
      {'type': 'error', 'message': 'Transport ended unexpectedly'}]
    assert _assistant_result(saved) == 'Useful report\n\nTransport ended unexpectedly'
  asyncio.run(scenario())


def _child(text=None, handback=None):
  from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
  block = (ToolUseBlock(id='handback', name='SubagentHandback', input={'message': handback})
           if handback is not None else TextBlock(text=text))
  return AssistantMessage(content=[block], model='claude', parent_tool_use_id='launch')


@pytest.mark.parametrize('handback', [False, True])
@pytest.mark.parametrize('summary', [None, 'Lifecycle summary, not the report'])
def test_claude_result_comes_from_ordered_child_stream(tmp_path, handback, summary):
  from claude_agent_sdk.types import AssistantMessage, TaskNotificationMessage, TextBlock
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  host._turn_by_agent['agent-1'] = turn
  host._turn_by_tool_use['launch'] = turn
  host._closed = True

  def end(uuid):
    return TaskNotificationMessage(subtype='task_notification', data={}, task_id='agent-1',
      status='completed', output_file='', summary=summary, uuid=uuid, session_id='host')

  class Stream:
    async def receive_messages(self):
      yield AssistantMessage(content=[TextBlock(text='Dispatcher narration')], model='claude')
      yield _child('Progress ' * 1000)
      yield _child(handback='Full report') if handback else _child('Full report')
      if handback:
        yield _child('Closing text')
      yield end('end')
      yield end('duplicate-end')
  host._client = Stream()
  asyncio.run(host._read())
  assert [e for e in events if e['type'] == 'assistant_result'] == [
    {'type': 'assistant_result', 'content': 'Full report'}]
  assert turn.summary == summary


def test_claude_handback_report_survives_a_failed_task(tmp_path):
  from claude_agent_sdk.types import TaskNotificationMessage
  host = _claude_host(tmp_path)
  turn = _turn(tmp_path)
  turn.started.set()
  events = []
  turn.sink = SimpleNamespace(publish=events.append)
  host._turn_by_agent['agent-1'] = turn
  host._turn_by_tool_use['launch'] = turn
  host._closed = True

  class Stream:
    async def receive_messages(self):
      yield _child(handback='Unsent report')
      yield _child('Done')
      yield TaskNotificationMessage(subtype='task_notification', data={}, task_id='agent-1',
        status='failed', output_file='', summary='Native handback failed', uuid='end',
        session_id='host')
  host._client = Stream()
  asyncio.run(host._read())
  assert turn.status == 'failed'
  assert [e for e in events if e['type'] == 'assistant_result'] == [
    {'type': 'assistant_result', 'content': 'Unsent report'}]
