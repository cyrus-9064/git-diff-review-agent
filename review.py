"""AI code-review agent for your uncommitted git changes.

Fixed workflow (plain code):  git diff -> split by file -> write review.md
Agent part (the model decides): for each file, the model may call read-only
tools (read_file, search_code) to check context before it gives its answer.

Usage:
    python review.py practice            # review the repo in ./practice
    python review.py path/to/any/repo    # review any git repo
    python review.py practice --model llama3.1:8b
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from openai import OpenAI

HERE = Path(__file__).resolve().parent

MAX_STEPS = 5            # the agent may use tools at most this many times per file
MAX_DIFF_CHARS = 12000   # longer diffs are cut so the model is not flooded
MAX_TOOL_CHARS = 5000    # tool results are cut for the same reason
MODEL_TRIES = 3          # each model call is tried up to this many times before giving up


# ---------------------------------------------------------------- settings
def load_env():
    """Read KEY=VALUE lines from a .env file next to this script (optional)."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

CONFIG = {
    "base_url": os.getenv("BASE_URL") or "http://localhost:11434/v1",
    "api_key": os.getenv("API_KEY") or "ollama",
    "model": os.getenv("MODEL") or "qwen2.5:7b",
}


def get_client():
    return OpenAI(base_url=CONFIG["base_url"], api_key=CONFIG["api_key"])


# ------------------------------------------------------- fixed workflow part
def run_git(repo, *args, check=True):
    """Run a git command inside `repo` and return the finished process."""
    result = subprocess.run(
        ["git", "-c", "user.name=review-agent", "-c", "user.email=review-agent@example.com", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def get_diff(repo):
    """All uncommitted changes to tracked files (staged and unstaged)."""
    return run_git(repo, "diff", "HEAD").stdout


def split_by_file(diff):
    """Cut one big diff into [(file_name, diff_for_that_file), ...]."""
    files = []
    for part in re.split(r"(?m)^(?=diff --git )", diff):
        if not part.startswith("diff --git "):
            continue
        header = part.split("\n", 1)[0]
        name = header.rsplit(" b/", 1)[-1].strip()
        files.append((name, part))
    return files


# --------------------------------------------------------------- agent tools
def make_tools(repo):
    """Two READ-ONLY tools the model may call. Nothing here can change files."""
    root = Path(repo).resolve()

    def safe_path(rel):
        full = (root / rel).resolve()
        if full != root and root not in full.parents:
            raise ValueError("path is outside the repo")
        parts = full.relative_to(root).parts
        if ".git" in parts or (parts and parts[-1].startswith(".env")):
            raise ValueError("that file is off limits")
        return full

    # Models sometimes send extra or renamed arguments (e.g. "query" instead of "text").
    # Instead of failing, the tools accept the usual alternatives and ignore the rest.
    def read_file(path=None, **extra):
        path = path or extra.get("file") or extra.get("file_path") or extra.get("filename")
        if not path:
            return "error: give the file to read in the argument 'path'"
        full = safe_path(path)
        if not full.is_file():
            return f"file not found: {path}"
        return full.read_text(encoding="utf-8", errors="replace")[:MAX_TOOL_CHARS]

    def search_code(text=None, **extra):
        text = text or extra.get("query") or extra.get("pattern") or extra.get("search")
        if not text:
            return "error: give the exact text to search for in the argument 'text'"
        result = run_git(root, "grep", "-n", "-F", "--", text, check=False)
        return result.stdout[:MAX_TOOL_CHARS] or "no matches"

    return {"read_file": read_file, "search_code": search_code}


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file in the repo. Only argument: path (required), relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search all tracked files in the repo for some exact text. Only argument: text (required).",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are a careful code reviewer. You are given the git diff of one file. "
    "Before judging, use the tools to read related code, for example the "
    "definition of any function the change calls. Use at most 3 tool calls in "
    "total, then give your answer. Report only real bugs or "
    "risks you are fairly sure about, one per bullet, naming the line. "
    "Ignore style. If you find nothing, reply with exactly: No problems found."
)


# ------------------------------------------------------------ the agent loop
def review_file(repo, name, diff, client=None, model=None, verbose=True):
    """Ask the model to review one file's diff. It may use tools along the way."""
    client = client or get_client()
    model = model or CONFIG["model"]
    tools = make_tools(repo)

    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff cut for length)"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Review this change to {name}:\n\n{diff}"},
    ]

    for step in range(MAX_STEPS):
        last_step = step == MAX_STEPS - 1
        if last_step:  # out of tool rounds: make the model answer instead of asking for more tools
            messages.append({"role": "user", "content": "You have used all your tool calls. "
                             "Give your final review now, in plain text. Do not call any tools."})
        resp, error = None, None
        for attempt in range(MODEL_TRIES):
            # on the last step forbid tools, but if the provider rejects that, try once without it
            extra = {"tool_choice": "none"} if last_step and attempt < MODEL_TRIES - 1 else {}
            try:
                resp = client.chat.completions.create(model=model, messages=messages,
                                                      tools=TOOL_SPECS, **extra)
                break
            except Exception as e:  # server down, rate limit, model sent a bad tool call...
                error = e
                if attempt < MODEL_TRIES - 1:
                    # a rate limit needs a longer rest; other errors are often one-off, so retry quickly
                    limited = "429" in str(e) or "rate" in str(e).lower()
                    time.sleep(10 if limited else 1)
        if resp is None:
            return f"Model error: {error}"

        msg = resp.choices[0].message
        calls = msg.tool_calls

        if not calls:  # no tool wanted -> this is the final answer
            return (msg.content or "").strip() or "No answer from the model."
        if last_step:  # it still wants tools after being told to answer: give up
            break

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in calls
                ],
            }
        )

        for call in calls:
            fname = call.function.name
            raw_args = call.function.arguments or "{}"
            if verbose:
                print(f"    tool call: {fname} {raw_args}")
            try:
                if fname not in tools:
                    result = f"unknown tool: {fname}"
                else:
                    result = tools[fname](**json.loads(raw_args))
            except Exception as e:  # bad JSON, bad path, ... -> tell the model, don't crash
                result = f"tool error: {e}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    return "Stopped: step limit reached before the model gave an answer."


# ------------------------------------------------------------------ the CLI
def main():
    parser = argparse.ArgumentParser(description="Review uncommitted git changes with an AI agent.")
    parser.add_argument("repo", nargs="?", default=".", help="path to a git repo (default: current folder)")
    parser.add_argument("--model", help="model name (overrides MODEL in .env)")
    parser.add_argument("--out", default="review.md", help="report file to write (default: review.md)")
    args = parser.parse_args()

    if args.model:
        CONFIG["model"] = args.model
    repo = os.path.abspath(args.repo)

    try:
        diff = get_diff(repo)
    except RuntimeError as e:
        sys.exit(f"Could not read the diff. Is '{repo}' a git repo with at least one commit?\n{e}")

    if not diff.strip():
        print("No uncommitted changes found. Edit a tracked file first, then run again.")
        return

    files = split_by_file(diff)
    print(f"Model: {CONFIG['model']}  |  files to review: {len(files)}")

    report = [f"# Code review\n\nModel: `{CONFIG['model']}`  \nFiles reviewed: {len(files)}\n"]
    for name, file_diff in files:
        print(f"Reviewing {name} ...")
        answer = review_file(repo, name, file_diff)
        if answer.startswith("Model error:"):
            sys.exit(f"{answer}\nIs your model running, and are BASE_URL / API_KEY / MODEL in .env correct?")
        report.append(f"## {name}\n\n{answer}\n")

    Path(args.out).write_text("\n".join(report), encoding="utf-8")
    print(f"Saved {args.out}")


if __name__ == "__main__":
    main()