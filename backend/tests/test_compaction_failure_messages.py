"""Known handoff refusals are actionable without exposing provider data."""

import json

import pytest

from app import compaction, models


CASES = [
  ("Your workspace is out of credits. Add credits to continue.", "Add credits"),
  ("insufficient_quota", "Add credits"),
  ("insufficient_credits", "Add credits"),
  ("You've hit your weekly limit · resets Jan 1, 12am (UTC)", "allowance"),
  ("usage_limit_reached", "allowance"),
  ("rate_limit_exceeded", "allowance"),
  ("Too many requests", "allowance"),
  ("Invalid authentication credentials", "Reconnect"),
  ("authentication_failed", "Reconnect"),
  ("invalid_api_key", "Reconnect"),
  ("Unauthorized", "Reconnect"),
  ("not logged in", "Reconnect"),
]


@pytest.mark.parametrize("cause,action", CASES)
@pytest.mark.parametrize("channel", ["error", "turn.failed", "string-error", "stderr"])
def test_known_codex_refusals_explain_next_step_without_echoing_data(cause, action, channel):
  private_error = cause + " secret-credential-and-transcript"
  if channel == "stderr":
    stdout, stderr = b"", private_error.encode()
  else:
    stdout = json.dumps({
      "type": "error" if channel == "string-error" else channel,
      "error": private_error if channel == "string-error" else {"message": private_error},
    }).encode()
    stderr = b"secret-stderr"
  message = compaction._codex_compaction_failure(stdout, stderr)
  assert action in message
  assert "conversation is unchanged" in message
  assert "secret" not in message


@pytest.mark.parametrize("cause,action", CASES)
def test_assistant_prose_is_not_failure_evidence(cause, action):
  stdout = json.dumps({"type": "agent_message", "text": cause}).encode()
  assert compaction._codex_compaction_failure(stdout, b"") == (
    "The incoming provider could not compact the chat."
  )


def test_structured_failure_takes_precedence_over_unrelated_stderr():
  stdout = b'{"type":"turn.failed","error":{"message":"unknown failure"}}'
  assert compaction._codex_compaction_failure(stdout, b"Unauthorized secret") == (
    "The incoming provider could not compact the chat."
  )


@pytest.mark.parametrize("cause,action", CASES)
def test_known_refusal_reaches_switch_response_without_changing_chat(
  client, auth, db, monkeypatch, cause, action,
):
  monkeypatch.setattr("app.providers.CodexProvider.check_auth", lambda *_args: None)

  async def refuse(_messages, **_kwargs):
    stdout = json.dumps({"type": "error", "message": cause}).encode()
    raise compaction.CompactionError(compaction._codex_compaction_failure(stdout, b""))

  monkeypatch.setattr(compaction, "summarize_chat", refuse)
  chat_id = client.post("/api/chats", json={"title": "Preserve me"}, headers=auth).json()["id"]
  source = [
    {"role": "user", "content": "Keep my history"},
    {"role": "assistant", "content": "Original answer"},
  ]
  client.put(f"/api/chats/{chat_id}", json={"messages": source}, headers=auth)
  row = db.get(models.Chat, chat_id)
  row.provider = "claude"
  row.session_id = "original-session"
  row.agent_settings_json = {"model": "claude-sonnet-4-6"}
  before = list(row.messages)
  db.commit()

  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json={
      "switch_id": "known-refusal", "provider": "codex",
      "agent_settings_json": {"model": "gpt-5.4", "effort": "high"},
    },
  )
  assert response.status_code == 422
  assert action in response.json()["detail"]
  db.expire_all()
  row = db.get(models.Chat, chat_id)
  assert row.provider == "claude"
  assert row.session_id == "original-session"
  assert row.agent_settings_json == {"model": "claude-sonnet-4-6"}
  assert row.messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["result", "errors", "assistant-error", "status"])
@pytest.mark.parametrize("cause,action,status", [
  ("Your workspace is out of credits.", "Add credits", None),
  ("You've hit your weekly limit", "allowance", 429),
  ("Invalid authentication credentials", "Reconnect", 401),
])
async def test_claude_error_terminal_is_actionable_and_discards_partial_text(
  monkeypatch, tmp_path, source, cause, action, status,
):
  from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

  if source == "status" and status is None:
    pytest.skip("Credit exhaustion has no unambiguous HTTP status.")
  terminal = ResultMessage(
    subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
    is_error=True, num_turns=1, session_id="private-session",
    result=cause + " secret-provider-data" if source == "result" else None,
    errors=[cause + " secret-provider-data"] if source == "errors" else None,
    api_error_status=status if source == "status" else None,
  )

  class Provider:
    def build_env(self, **_kwargs):
      return {}

  class Client:
    disconnected = False

    async def connect(self):
      pass

    async def query(self, _prompt):
      pass

    async def receive_response(self):
      yield AssistantMessage(
        content=[TextBlock(text=cause if source == "assistant-error" else "partial-secret")],
        model="claude", error="unknown" if source == "assistant-error" else None,
      )
      yield terminal

    async def disconnect(self):
      self.disconnected = True

  client = Client()
  monkeypatch.setattr("app.providers.get_provider", lambda _pid: Provider())
  monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", lambda _opts: client)
  with pytest.raises(compaction.CompactionError) as failure:
    await compaction._run_claude_summarize_turn(
      "private prompt", data_dir=str(tmp_path), model=None, effort=None,
    )
  assert action in str(failure.value)
  assert "secret" not in str(failure.value)
  assert client.disconnected
