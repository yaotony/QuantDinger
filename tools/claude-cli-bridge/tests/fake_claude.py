"""Stand-in for the Claude Code CLI used by the test suite.

Behaviour is selected with FAKE_CLAUDE_SCENARIO. Each invocation appends its
argv and stdin to the JSONL file named by FAKE_CLAUDE_RECORD.
"""
from __future__ import annotations

import json
import os
import sys
import time


def arg_value(argv: list[str], flag: str, default: str = "") -> str:
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


def result_payload(text: str, model: str, *, is_error: bool = False) -> dict:
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "result": text,
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "output_tokens": 5},
        "modelUsage": {} if is_error else {f"claude-{model}-test": {}},
    }


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.read()
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd()}) + "\n")

    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "echo")
    model = arg_value(argv, "--model", "sonnet")
    output_format = arg_value(argv, "--output-format", "text")

    if scenario == "sleep":
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "30")))
    if scenario == "crash":
        print("segfault-ish failure", file=sys.stderr)
        return 1
    if scenario == "error":
        message = os.environ.get("FAKE_CLAUDE_ERROR", "API Error: something failed")
        print(json.dumps(result_payload(message, model, is_error=True)))
        return 1
    if scenario == "garbage":
        print("not json at all")
        return 0

    text = os.environ.get("FAKE_CLAUDE_TEXT") or f"ECHO:{stdin}"
    if output_format == "stream-json":
        chunks = [text[i : i + 4] for i in range(0, len(text), 4)] or [""]
        print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
        for chunk in chunks:
            event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}
            print(json.dumps({"type": "stream_event", "event": event}), flush=True)
            delay = float(os.environ.get("FAKE_CLAUDE_CHUNK_DELAY", "0"))
            if delay:
                time.sleep(delay)
        print(json.dumps(result_payload(text, model)), flush=True)
        return 0
    print(json.dumps(result_payload(text, model)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
