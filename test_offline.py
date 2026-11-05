import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

import review


# ----------------------------------------------------------- fake model kit
def tool_call(call_id, name, args):
    arguments = args if isinstance(args, str) else json.dumps(args)
    return NS(id=call_id, function=NS(name=name, arguments=arguments))


def reply(content=None, calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))])


class FakeClient:
    """Returns the prepared replies one by one (the last one repeats forever)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        nxt = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def make_repo(tmp):
    repo = Path(tmp)
    review.run_git(repo, "init")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=123\n", encoding="utf-8")
    review.run_git(repo, "add", ".")
    review.run_git(repo, "commit", "-m", "first")
    return repo


# -------------------------------------------------------------------- tests
def test_split_by_file():
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n+x\n"
            "diff --git a/dir/b.py b/dir/b.py\n--- a/dir/b.py\n+++ b/dir/b.py\n+y\n")
    files = review.split_by_file(diff)
    assert [name for name, _ in files] == ["a.py", "dir/b.py"]
    assert files[1][1].startswith("diff --git a/dir/b.py")


def test_get_diff_sees_uncommitted_change():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = make_repo(tmp)
        assert review.get_diff(repo) == ""
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        files = review.split_by_file(review.get_diff(repo))
        assert [n for n, _ in files] == ["calc.py"]


def test_tools_are_safe_and_read_only():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = make_repo(tmp)
        tools = review.make_tools(repo)
        assert "def add" in tools["read_file"]("calc.py")
        assert tools["read_file"]("nope.py").startswith("file not found")
        for bad in ["../outside.txt", ".env", ".git/config"]:
            try:
                tools["read_file"](bad)
                raise AssertionError(f"should have refused {bad}")
            except ValueError:
                pass
        assert "calc.py" in tools["search_code"]("def add")
        assert tools["search_code"]("zzz_not_here") == "no matches"
        assert set(tools) == {"read_file", "search_code"}  # nothing that writes


def test_agent_uses_tool_then_answers():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = make_repo(tmp)
        client = FakeClient([
            reply(calls=[tool_call("1", "read_file", {"path": "calc.py"})]),
            reply(content="- Bug: add() now subtracts."),
        ])
        answer = review.review_file(str(repo), "calc.py", "diff...", client=client, verbose=False)
        assert answer == "- Bug: add() now subtracts."
        last = client.requests[-1][-1]          # the tool result sent back to the model
        assert last["role"] == "tool" and "def add" in last["content"]


def test_step_limit_stops_a_looping_agent():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = make_repo(tmp)
        client = FakeClient([reply(calls=[tool_call("1", "read_file", {"path": "calc.py"})])])
        answer = review.review_file(str(repo), "calc.py", "diff...", client=client, verbose=False)
        assert answer.startswith("Stopped")
        assert len(client.requests) == review.MAX_STEPS


def test_bad_tool_calls_do_not_crash():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = make_repo(tmp)
        client = FakeClient([
            reply(calls=[tool_call("1", "read_file", "this is not json"),
                         tool_call("2", "delete_everything", {}),
                         tool_call("3", "read_file", {"path": "../../etc/passwd"})]),
            reply(content="No problems found."),
        ])
        answer = review.review_file(str(repo), "calc.py", "diff...", client=client, verbose=False)
        assert answer == "No problems found."
        results = [m["content"] for m in client.requests[-1] if m["role"] == "tool"]
        assert results[0].startswith("tool error")
        assert results[1].startswith("unknown tool")
        assert "outside the repo" in results[2]


def test_model_error_is_reported_not_raised():
    client = FakeClient([RuntimeError("connection refused")])
    answer = review.review_file(".", "x.py", "diff...", client=client, verbose=False)
    assert answer.startswith("Model error:") and "connection refused" in answer


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"\nAll {len(tests)} offline tests passed.")
