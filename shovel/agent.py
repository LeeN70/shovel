"""Claude Agent SDK integration for Shovel."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from shovel.prompt import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from shovel.utils import detect_language, get_modified_files

logger = logging.getLogger(__name__)


def _sdk_symbols() -> dict[str, Any]:
    """Load SDK symbols lazily so CLI help works without optional deps."""
    from claude_agent_sdk import (  # type: ignore
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    return {
        "query": query,
        "ClaudeAgentOptions": ClaudeAgentOptions,
        "ResultMessage": ResultMessage,
        "AssistantMessage": AssistantMessage,
        "UserMessage": UserMessage,
        "SystemMessage": SystemMessage,
        "TextBlock": TextBlock,
        "ThinkingBlock": ThinkingBlock,
        "ToolUseBlock": ToolUseBlock,
        "ToolResultBlock": ToolResultBlock,
    }


def _serialize_content_block(block: Any, sdk: dict[str, Any]) -> dict:
    """Serialize a content block to a JSON-friendly dict."""
    if isinstance(block, sdk["TextBlock"]):
        return {"type": "text", "text": block.text}
    if isinstance(block, sdk["ThinkingBlock"]):
        return {"type": "thinking", "thinking": block.thinking}
    if isinstance(block, sdk["ToolUseBlock"]):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, sdk["ToolResultBlock"]):
        content = block.content
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": content,
            "is_error": block.is_error,
        }
    return {"type": "unknown", "repr": repr(block)}


def _serialize_message(message: Any, sdk: dict[str, Any]) -> dict | None:
    """Serialize a message to a JSON-friendly dict."""
    if isinstance(message, sdk["AssistantMessage"]):
        return {
            "role": "assistant",
            "model": message.model,
            "content": [_serialize_content_block(b, sdk) for b in message.content],
        }
    if isinstance(message, sdk["UserMessage"]):
        if isinstance(message.content, str):
            content = message.content
        else:
            content = [_serialize_content_block(b, sdk) for b in message.content]
        return {"role": "user", "content": content}
    if isinstance(message, sdk["SystemMessage"]):
        return {"role": "system", "subtype": message.subtype, "data": message.data}
    if isinstance(message, sdk["ResultMessage"]):
        return {
            "role": "result",
            "subtype": message.subtype,
            "is_error": message.is_error,
            "num_turns": message.num_turns,
            "duration_ms": message.duration_ms,
            "duration_api_ms": message.duration_api_ms,
            "total_cost_usd": message.total_cost_usd,
            "usage": message.usage,
            "session_id": message.session_id,
        }
    return None


def _summarize_tool_input(tool_name: str, input_data: dict) -> str:
    """Create a short summary of tool input for logging."""
    if tool_name == "Bash":
        cmd = input_data.get("command", "")
        return cmd.strip().split("\n")[0][:120]
    if tool_name == "Read":
        return input_data.get("file_path", "")
    if tool_name == "Write":
        path = input_data.get("file_path", "")
        content = input_data.get("content", "")
        return f"{path} ({len(content)} chars)"
    if tool_name == "Edit":
        path = input_data.get("file_path", "")
        old = input_data.get("old_string", "")
        return f"{path} (replacing {len(old)} chars)"
    if tool_name == "Glob":
        return input_data.get("pattern", "")
    if tool_name == "Grep":
        pattern = input_data.get("pattern", "")
        path = input_data.get("path", ".")
        return f"'{pattern}' in {path}"
    if tool_name == "NotebookEdit":
        path = input_data.get("notebook_path", "")
        mode = input_data.get("edit_mode", "replace")
        return f"{path} ({mode})"
    if tool_name == "WebFetch":
        return input_data.get("url", "")[:120]
    if tool_name == "WebSearch":
        return input_data.get("query", "")
    if tool_name == "TodoWrite":
        return f"{len(input_data.get('todos', []))} todos"
    if tool_name == "BashOutput":
        return f"shell={input_data.get('bash_id', '')}"
    if tool_name == "KillBash":
        return f"shell={input_data.get('shell_id', '')}"
    return str(input_data)[:100]


def build_user_prompt(instance: dict, build_dir: str) -> str:
    """Build the user prompt from an SWE-bench instance."""
    test_patch = instance.get("test_patch", "")
    patch = instance.get("patch", "")
    test_files = get_modified_files(test_patch)
    language = detect_language(test_files)

    problem_statement = instance.get("problem_statement", "")

    test_files_list = "\n".join(f"- `{f}`" for f in test_files) if test_files else "- (none detected)"

    return USER_PROMPT_TEMPLATE.format(
        repo=instance["repo"],
        instance_id=instance["instance_id"],
        base_commit=instance["base_commit"],
        language=language,
        problem_statement=problem_statement,
        test_patch=test_patch,
        test_files_list=test_files_list,
        patch=patch,
        build_dir=build_dir,
    )


def _parse_output_from_final_assistant_text(text: str) -> dict | None:
    """Parse the final assistant message and extract output JSON."""
    marker_match = re.search(
        r"<SHOVEL_OUTPUT_JSON>\s*```json\s*(\{.*?\})\s*```\s*</SHOVEL_OUTPUT_JSON>",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    candidates: list[str] = []
    if marker_match:
        candidates.append(marker_match.group(1))

    fenced_match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        candidates.append(fenced_match.group(1))

    candidates.append(text.strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def run_agent(
    instance: dict,
    repo_dir: str,
    model: str,
    max_turns: int = 50,
    log_dir: str | None = None,
    project_dir: str = ".",
) -> dict | None:
    """Run Claude agent to generate Docker configuration."""
    sdk = _sdk_symbols()
    instance_id = instance["instance_id"]
    build_dir = os.path.join(os.path.abspath(project_dir), "tmp", f"docker_build_{instance_id}")
    user_prompt = build_user_prompt(instance, build_dir)

    options = sdk["ClaudeAgentOptions"](
        model=model,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "NotebookEdit",
            "WebFetch",
            "WebSearch",
            "TodoWrite",
            "BashOutput",
            "KillBash",
        ],
        permission_mode="bypassPermissions",
        cwd=repo_dir,
        max_turns=max_turns,
    )

    logger.info("[%s] Starting agent (model=%s, cwd=%s)", instance_id, model, repo_dir)
    start_time = time.time()
    log_file = _open_trajectory_log(instance_id, user_prompt, log_dir, start_time)

    result_message = None
    last_assistant_text = None
    turn_count = 0
    try:
        async for message in sdk["query"](prompt=user_prompt, options=options):
            serialized = _serialize_message(message, sdk)
            if serialized is not None:
                _append_to_log(log_file, serialized)

            if isinstance(message, sdk["ResultMessage"]):
                result_message = message
                break
            if isinstance(message, sdk["AssistantMessage"]):
                turn_count += 1
                text_blocks = []
                for block in message.content:
                    if isinstance(block, sdk["TextBlock"]):
                        text_blocks.append(block.text)
                        first_line = block.text.strip().split("\n")[0][:150]
                        logger.info("[%s] [turn %s] TEXT: %s", instance_id, turn_count, first_line)
                    elif isinstance(block, sdk["ToolUseBlock"]):
                        input_summary = _summarize_tool_input(block.name, block.input)
                        logger.info(
                            "[%s] [turn %s] TOOL: %s(%s)",
                            instance_id,
                            turn_count,
                            block.name,
                            input_summary,
                        )
                if text_blocks:
                    last_assistant_text = "\n".join(text_blocks)
            elif isinstance(message, sdk["UserMessage"]) and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, sdk["ToolResultBlock"]) and block.is_error:
                        err_preview = str(block.content)[:150] if block.content else ""
                        logger.warning("[%s] TOOL_ERROR: %s", instance_id, err_preview)
    except Exception as exc:
        logger.error("[%s] Agent error: %s", instance_id, exc)
        _append_to_log(log_file, {"role": "error", "error": str(exc)})
        _close_trajectory_log(log_file, start_time)
        return None

    _close_trajectory_log(log_file, start_time)

    output = None
    if result_message is not None:
        if result_message.is_error:
            logger.error("[%s] Agent returned error: %s", instance_id, result_message.result)
            return None
    if last_assistant_text is not None:
        output = _parse_output_from_final_assistant_text(last_assistant_text)
        if output is not None:
            logger.info("[%s] Parsed output JSON from final assistant message", instance_id)

    if output is None:
        logger.error("[%s] Failed to parse output JSON from final assistant message", instance_id)
        return None

    if not isinstance(output, dict):
        logger.error("[%s] Parsed output is not a dict: %s", instance_id, type(output))
        return None

    required_keys = ["dockerfile", "eval_script", "setup_scripts"]
    for key in required_keys:
        if key not in output:
            logger.error("[%s] Missing key in output: %s", instance_id, key)
            return None

    if "setup_repo.sh" not in output.get("setup_scripts", {}):
        logger.error("[%s] Missing setup_repo.sh in setup_scripts", instance_id)
        return None

    if "OMNIGRIL_EXIT_CODE" not in output["eval_script"]:
        logger.warning("[%s] eval_script missing OMNIGRIL_EXIT_CODE, injecting...", instance_id)
        output["eval_script"] = (
            output["eval_script"].rstrip() + '\nrc=$?\necho "OMNIGRIL_EXIT_CODE=$rc"\n'
        )

    if result_message is None:
        logger.warning(
            "[%s] Completed without ResultMessage; using AssistantMessage turn count",
            instance_id,
        )

    cost = result_message.total_cost_usd if result_message is not None else None
    turns = result_message.num_turns if result_message is not None else turn_count
    elapsed = time.time() - start_time
    elapsed_str = f"{elapsed:.1f}s"
    if elapsed >= 60:
        elapsed_str = f"{int(elapsed // 60)}m{int(elapsed % 60)}s"
    if cost:
        logger.info("[%s] Agent completed: %s turns, $%.4f, duration=%s", instance_id, turns, cost, elapsed_str)
    else:
        logger.info("[%s] Agent completed: %s turns, duration=%s", instance_id, turns, elapsed_str)

    return output


def _open_trajectory_log(instance_id: str, user_prompt: str, log_dir: str | None, start_time: float | None = None):
    """Open a JSONL trajectory log file and write the header line."""
    if log_dir is None:
        return None
    os.makedirs(log_dir, exist_ok=True)
    safe_id = instance_id.replace("/", "__")
    log_path = os.path.join(log_dir, f"{safe_id}.jsonl")
    try:
        handle = open(log_path, "w")
        header = {
            "type": "header",
            "instance_id": instance_id,
            "user_prompt": user_prompt,
            "start_time": start_time,
            "start_time_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
            if start_time
            else None,
        }
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        handle.flush()
        logger.info("[%s] Trajectory logging to %s", instance_id, log_path)
        return handle
    except Exception as exc:
        logger.error("[%s] Failed to open trajectory log: %s", instance_id, exc)
        return None


def _append_to_log(log_file: Any, data: dict) -> None:
    """Append a single message to trajectory file."""
    if log_file is None:
        return
    try:
        log_file.write(json.dumps(data, ensure_ascii=False) + "\n")
        log_file.flush()
    except Exception:
        pass


def _close_trajectory_log(log_file: Any, start_time: float | None = None) -> None:
    """Write footer and close trajectory log file."""
    if log_file is None:
        return
    try:
        end_time = time.time()
        duration_seconds = round(end_time - start_time, 2) if start_time else None
        footer = {
            "type": "footer",
            "end_time": end_time,
            "end_time_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
            "duration_seconds": duration_seconds,
        }
        log_file.write(json.dumps(footer, ensure_ascii=False) + "\n")
        log_file.flush()
        log_file.close()
    except Exception:
        pass
