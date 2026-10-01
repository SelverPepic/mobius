"""Quiet delivery through the existing run-bound control worker.

The stream carries intent, never credentials or a choice of caller. The host
supplies the exact run environment and the reviewed eligible tool catalogue.
The control worker still owns every handler and permission check. Its result
settles the durable journal/UI only, never a provider tool-result message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence

from app.agent_write_delivery import WriteOutcome
from app.app_tools import AppTool
from app.platform_tools import (
  CONTROL_TOOL_TIMEOUT_SECONDS, RESULT_INDEPENDENT_CONTROL_TOOLS,
  _control_script, expected_control_tool_names,
)
from app.process_groups import terminate_process_group

log = logging.getLogger(__name__)
MAX_RECEIPT_BYTES = 1024 * 1024


def eligible_tool_names(*, app_tools: Sequence[AppTool], top_level: bool,
                        coordination_enabled: bool = True) -> frozenset[str]:
  available = expected_control_tool_names(top_level=top_level,
    coordination_enabled=coordination_enabled)
  return frozenset(name for name in available
                   if name in RESULT_INDEPENDENT_CONTROL_TOOLS) | frozenset(
    tool.exposed_name for tool in app_tools if tool.result_independent)


async def _bounded_read(stream, limit):
  pieces, size = [], 0
  while chunk := await stream.read(min(65536, limit + 1 - size)):
    size += len(chunk)
    if size > limit:
      raise ValueError("Control receipt exceeded its bound")
    pieces.append(chunk)
  return b"".join(pieces)


async def _cleanup(process, pgid, readers):
  for reader in readers:
    if not reader.done():
      reader.cancel()
  await asyncio.gather(*readers, return_exceptions=True)
  if process is None:
    return
  await asyncio.to_thread(terminate_process_group, pgid, logger=log,
                          label="quiet control")
  if process.returncode is None:
    process.kill()
  process.stdin.close()
  async def discard(stream):
    while await stream.read(65536):
      pass
  # wait() alone doesn't close unread pipe transports after bounded-read
  # overflow. Drain after termination without accumulating more output.
  try:
    async with asyncio.timeout(3):
      await asyncio.gather(process.wait(), discard(process.stdout),
                           discard(process.stderr), process.stdin.wait_closed(),
                           return_exceptions=True)
  except TimeoutError:
    # A command that escaped its group may still hold an inherited pipe.
    # asyncio has no public close for Process/StreamReader transports. Close
    # only this privately launched process's pipes, never a shared handle.
    process._transport.close()


class QuietToolDispatcher:
  def __init__(self, *, env: Mapping[str, str], eligible_tools: frozenset[str],
               publish: Callable[[dict], None]):
    self.env = dict(env)
    # This dedicated worker already has its routed helper's complete env.
    # Never let the shared-host startup indirection select another identity.
    self.env.pop("MOBIUS_CALLER_ENV_FILE", None)
    self.env.pop("MOBIUS_HELPER_HOST", None)
    self.eligible_tools = eligible_tools
    self.publish = publish

  async def __call__(self, write: dict) -> WriteOutcome:
    name, arguments = write["tool"], write["arguments"]
    if name not in self.eligible_tools or "_mobius_caller_env_file" in arguments:
      return WriteOutcome("failed", "Quiet tool/caller override is not permitted")
    # This is a host operation, not a fictitious provider tool-call fork point.
    call_id = f"quiet:{write['run_id']}:{write['id']}"
    event = {"tool_use_id": call_id, "tool": f"mcp__mobius_control__{name}"}
    self.publish({**event, "type": "tool_start", "input": json.dumps(arguments, ensure_ascii=False),
                  "delivery": "quiet"})
    request = {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
      "params": {"name": name, "arguments": arguments,
        "_meta": {"mobius/writeOperationId": write["id"]}}}
    process = None
    pgid = None
    readers = []
    result = None
    outcome = WriteOutcome("unknown", "Tool completion could not be confirmed; inspect before retry")
    try:
      process = await asyncio.create_subprocess_exec(
        sys.executable, _control_script(), "--dispatch-message", env=self.env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
      )
      # start_new_session guarantees PID == PGID on our Unix runtime. Keep
      # that creation-time identity even if the leader exits before getpgid.
      pgid = process.pid
      async with asyncio.timeout(CONTROL_TOOL_TIMEOUT_SECONDS):
        readers = [asyncio.create_task(_bounded_read(process.stdout, MAX_RECEIPT_BYTES)),
                   asyncio.create_task(_bounded_read(process.stderr, MAX_RECEIPT_BYTES))]
        process.stdin.write(json.dumps(request, allow_nan=False).encode())
        await process.stdin.drain()
        process.stdin.close()
        stdout, _stderr = await asyncio.gather(*readers)
        await process.wait()
        response = json.loads(stdout)
        if (process.returncode != 0 or not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0" or response.get("id") != call_id
            or "error" in response):
          raise ValueError("Unconfirmed control completion")
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
          raise ValueError("Malformed control receipt")
        if result.get("isError") is True:
          detail = "\n".join(block["text"] for block in result["content"]
            if isinstance(block, dict) and isinstance(block.get("text"), str))
          # Never copy the run credential into either the journal or transcript.
          credential = self.env.get("AGENT_TOKEN")
          if credential:
            detail = detail.replace(credential, "[redacted]")
          outcome = WriteOutcome("failed", detail[:500] or "Tool reported a failure")
        elif result.get("isError", False) is False:
          outcome = WriteOutcome("succeeded")
        else:
          raise ValueError("Malformed control outcome")
    except asyncio.CancelledError:
      raise
    except Exception:
      # Raw stderr/exception text may include credentials; it is not evidence
      # that a side effect failed before execution and must not invite replay.
      result = None
    finally:
      # Also clean descendants after a normal leader exit; no detached command
      # may outlive this exact run's owning finish barrier.
      cleanup = asyncio.create_task(_cleanup(process, pgid, readers))
      cancelled = None
      while True:
        try:
          await asyncio.shield(cleanup)
          break
        except asyncio.CancelledError as exc:
          cancelled = exc
      content = (json.dumps(result, ensure_ascii=False) if result is not None
                 else outcome.reason)
      credential = self.env.get("AGENT_TOKEN")
      if credential and content:
        content = content.replace(credential, "[redacted]")
      self.publish({**event, "type": "tool_output", "content": content or "",
        "output_complete": True,
        "output_exit_code": 0 if outcome.status == "succeeded" else 1,
        "delivery": "quiet"})
      # Output completion is not lifecycle completion in either chat reducer.
      # This closes owner activity only; it never enters the provider stream.
      self.publish({**event, "type": "tool_end", "delivery": "quiet"})
      if cancelled is not None:
        raise cancelled
    return outcome
