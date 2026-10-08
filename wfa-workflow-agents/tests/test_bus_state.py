"""`wfa/bus.py` + `wfa/state.py` 单元测试：Agent 间通信与共享黑板。

覆盖点（对应 CONTRACT 第 3、4 节）：
    1. MessageBus：``publish`` / ``send`` / 收件箱 / ``BROADCAST`` / ``ask``+``reply``
       的 correlation_id 配对 / ``history`` 过滤 / ``to_dict``+``from_dict`` 往返 /
       ``log_lines``；``wait_for`` 支持"应答比等待先到"与真正阻塞两种情形，
       **超时返回 None**（特意选一个没有任何消息的 recipient，避免广播干扰）；
    2. Subscription：``drain(0)`` 立即返回、``drain(timeout)`` 阻塞取消息、
       ``close`` 幂等且唤醒等待者；
    3. Blackboard：``set/get/require/update/snapshot`` 与 ``ns_*`` 命名空间、
       ``put_artifact/artifact/artifacts``、``write_file/read_file/file_hash/file_exists/list_files``、
       ``record/bump_iteration/iteration/summary``、``save``/``load`` 往返一致；
       路径越界抛 ``PathSecurityError``。

全部离线、无外部依赖；阻塞类用例用 daemon 线程控制时序，超时上限给足余量。
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest

from tests.helpers import temp_workspace
from wfa.bus import BROADCAST, PERF_ID, Message, MessageBus, Subscription
from wfa.errors import NodeError, PathSecurityError
from wfa.state import Blackboard, NodeRecord, RunState


class MessageModelTest(unittest.TestCase):
    """Message 的自动字段与序列化。"""

    def test_auto_id_and_timestamp(self):
        message = Message(kind="task", sender="a", recipient="b")
        self.assertTrue(message.id.startswith("msg-"))
        self.assertTrue(message.created_at)
        self.assertEqual(message.payload, {})

    def test_non_dict_payload_is_wrapped(self):
        message = Message(kind="task", sender="a", recipient="b", payload=["x", "y"])
        self.assertEqual(message.payload, {"value": ["x", "y"]})

    def test_roundtrip_keeps_all_fields(self):
        message = Message(kind="answer", sender="b", recipient="a", payload={"n": 1},
                          turn=3, correlation_id="corr-1")
        restored = Message.from_dict(message.to_dict())
        self.assertEqual(restored.to_dict(), message.to_dict())

    def test_from_dict_tolerates_missing_fields(self):
        restored = Message.from_dict({})
        self.assertEqual(restored.kind, "")
        self.assertEqual(restored.payload, {})
        self.assertTrue(restored.id)

    def test_brief_is_one_line(self):
        brief = Message(kind="review", sender="reviewer", recipient="workflow").brief()
        self.assertIn("review", brief)
        self.assertIn("reviewer", brief)
        self.assertNotIn("\n", brief)


class MessageBusPublishTest(unittest.TestCase):
    """发布、收件箱与广播。"""

    def setUp(self):
        self.bus = MessageBus()

    def test_send_records_history(self):
        message = self.bus.send("task", "start", "coder", {"goal": "写代码"}, turn=1)
        self.assertEqual(message.kind, "task")
        self.assertEqual(len(self.bus.messages), 1)
        self.assertEqual(self.bus.history()[0].id, message.id)

    def test_inbox_contains_direct_messages_only(self):
        self.bus.send("task", "a", "bob")
        self.bus.send("task", "a", "alice")
        inbox = self.bus.inbox("bob")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].recipient, "bob")

    def test_inbox_includes_broadcast(self):
        self.bus.send("system", PERF_ID, BROADCAST, {"event": "turn_start"})
        self.bus.send("task", "a", "bob")
        self.assertEqual(len(self.bus.inbox("bob")), 2)
        self.assertEqual([m.kind for m in self.bus.inbox("whoever")], ["system"])

    def test_broadcast_reaches_all_subscribers(self):
        sub_a = self.bus.subscribe("a")
        sub_b = self.bus.subscribe("b")
        self.bus.send("system", PERF_ID, BROADCAST, {"event": "x"})
        self.assertEqual(len(sub_a.drain(0)), 1)
        self.assertEqual(len(sub_b.drain(0)), 1)
        self.assertEqual(self.bus.subscribers(), ["a", "b"])

    def test_subscribe_is_idempotent_and_unsubscribe_closes(self):
        first = self.bus.subscribe("a")
        self.assertIs(first, self.bus.subscribe("a"))
        self.bus.send("task", "x", "a")
        self.assertEqual(len(first.drain(0)), 1)

        self.bus.unsubscribe("a")
        self.assertTrue(first.closed)
        self.assertEqual(self.bus.subscribers(), [])
        self.bus.send("task", "x", "a")
        self.assertEqual(len(first.drain(0)), 0)
        self.bus.unsubscribe("a")            # 幂等

    def test_ask_reply_pairing(self):
        request = self.bus.ask("coder", "reviewer", {"question": "这段代码有问题吗？"}, turn=2)
        self.assertEqual(request.kind, "question")
        self.assertEqual(request.recipient, "reviewer")

        answer = self.bus.reply(request, "reviewer", {"issues": []})
        self.assertEqual(answer.kind, "answer")
        self.assertEqual(answer.recipient, "coder")
        self.assertEqual(answer.correlation_id, request.id)
        self.assertEqual(answer.turn, request.turn)

        self.assertEqual(len(self.bus.history(kind="question")), 1)
        self.assertEqual(len(self.bus.history(kind="answer")), 1)

    def test_history_filters_and_limit(self):
        for index in range(5):
            self.bus.send("task", "a" if index % 2 == 0 else "b", "target", {"i": index})
        self.assertEqual(len(self.bus.history(sender="a")), 3)
        self.assertEqual(len(self.bus.history(recipient="target")), 5)
        recent = self.bus.history(limit=2)
        self.assertEqual([m.payload["i"] for m in recent], [3, 4])

    def test_counts_and_to_dict(self):
        self.bus.send("task", "a", "b")
        self.bus.send("task", "a", "b")
        self.bus.send("result", "b", "a")
        self.assertEqual(self.bus.counts(), {"task": 2, "result": 1})
        payload = self.bus.to_dict()
        self.assertEqual(payload["counts"], {"task": 2, "result": 1})
        self.assertEqual(len(payload["messages"]), 3)
        json.dumps(payload, ensure_ascii=False)     # 必须可 JSON 序列化

    def test_from_dict_restores_history(self):
        self.bus.send("task", "a", "b", {"i": 1}, turn=1)
        self.bus.send("answer", "b", "a", {"ok": True}, correlation_id="c1")
        restored = MessageBus.from_dict(self.bus.to_dict())
        self.assertEqual(len(restored.messages), 2)
        self.assertEqual(restored.counts(), {"task": 1, "answer": 1})
        self.assertEqual(restored.inbox("b")[0].payload, {"i": 1})
        self.assertEqual(restored.messages[1].correlation_id, "c1")

    def test_from_dict_tolerates_garbage(self):
        self.assertEqual(len(MessageBus.from_dict({}).messages), 0)
        self.assertEqual(len(MessageBus.from_dict(None).messages), 0)

    def test_log_lines_readable(self):
        self.bus.send("question", "coder", "reviewer", {"q": "?"}, turn=1)
        self.bus.send("answer", "reviewer", "coder", {"a": "!"}, correlation_id="corr-9")
        lines = self.bus.log_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("#001", lines[0])
        self.assertIn("coder → reviewer", lines[0])
        self.assertIn("[corr=corr-9]", lines[1])

    def test_echo_mode_still_works(self):
        bus = MessageBus(echo=True)
        bus.send("system", PERF_ID, BROADCAST, {"event": "turn_start"})
        self.assertEqual(len(bus.messages), 1)


class SubscriptionTest(unittest.TestCase):
    """订阅队列：立即取、阻塞取、关闭。"""

    def test_drain_zero_returns_immediately(self):
        bus = MessageBus()
        sub = bus.subscribe("a")
        started = time.monotonic()
        self.assertEqual(sub.drain(0), [])
        self.assertLess(time.monotonic() - started, 0.5)

    def test_drain_returns_after_publish(self):
        bus = MessageBus()
        sub = bus.subscribe("a")

        def producer():
            time.sleep(0.15)
            bus.send("task", "x", "a", {"n": 1})

        threading.Thread(target=producer, daemon=True).start()
        messages = sub.drain(2.0)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].payload, {"n": 1})
        self.assertEqual(sub.pending, 0)

    def test_drain_takes_everything_queued(self):
        bus = MessageBus()
        sub = bus.subscribe("a")
        bus.send("task", "x", "a", {"n": 1})
        bus.send("task", "x", "a", {"n": 2})
        self.assertEqual([m.payload["n"] for m in sub.drain(0)], [1, 2])
        self.assertEqual(sub.drain(0), [])

    def test_close_wakes_waiter(self):
        bus = MessageBus()
        sub = bus.subscribe("a")
        results: list = []

        def waiter():
            results.extend(sub.drain(3.0))

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.1)
        sub.close()
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertTrue(sub.closed)

    def test_publish_after_close_is_dropped(self):
        bus = MessageBus()
        sub = bus.subscribe("a")
        bus.unsubscribe("a")
        bus.send("task", "x", "a", {"n": 1})
        self.assertEqual(sub.drain(0), [])
        self.assertIsInstance(sub, Subscription)


class WaitForTest(unittest.TestCase):
    """等待应答：先查历史再阻塞，超时返回 None。"""

    def test_existing_message_is_found_without_blocking(self):
        bus = MessageBus()
        # 模拟"应答比等待先到"的竞态
        bus.send("answer", "reviewer", "coder", {"ok": True}, correlation_id="c1")
        started = time.monotonic()
        message = bus.wait_for("coder", kind="answer", correlation_id="c1", timeout=0.0)
        self.assertIsNotNone(message)
        self.assertEqual(message.correlation_id, "c1")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_message_is_consumed_only_once(self):
        bus = MessageBus()
        bus.send("answer", "b", "a", {"n": 1})
        self.assertIsNotNone(bus.wait_for("a", timeout=0.0))
        self.assertIsNone(bus.wait_for("a", timeout=0.0))

    def test_timeout_returns_none_for_silent_recipient(self):
        bus = MessageBus()
        bus.send("task", "x", "someone-else", {"n": 1})   # 不能落在 recipient 的收件箱里
        started = time.monotonic()
        self.assertIsNone(bus.wait_for("nobody", timeout=0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.1)

    def test_zero_timeout_returns_none_immediately(self):
        bus = MessageBus()
        self.assertIsNone(bus.wait_for("nobody", timeout=0.0))

    def test_kind_filter_is_respected(self):
        bus = MessageBus()
        bus.send("task", "x", "a", {"n": 1})
        bus.send("answer", "b", "a", {"n": 2})
        message = bus.wait_for("a", kind="answer", timeout=0.0)
        self.assertIsNotNone(message)
        self.assertEqual(message.kind, "answer")
        self.assertEqual(message.payload, {"n": 2})

    def test_blocks_until_message_arrives(self):
        bus = MessageBus()

        def producer():
            time.sleep(0.15)
            bus.send("answer", "b", "a", {"ok": True})

        threading.Thread(target=producer, daemon=True).start()
        message = bus.wait_for("a", kind="answer", timeout=3.0)
        self.assertIsNotNone(message)
        self.assertTrue(message.payload["ok"])

    def test_broadcast_is_visible_to_wait_for(self):
        bus = MessageBus()
        bus.send("system", PERF_ID, BROADCAST, {"event": "stop"})
        message = bus.wait_for("worker-1", kind="system", timeout=0.0)
        self.assertIsNotNone(message)
        self.assertEqual(message.payload["event"], "stop")


class BlackboardKeyValueTest(unittest.TestCase):
    """黑板键值与命名空间。"""

    def test_set_get_default_and_require(self):
        board = Blackboard()
        board.set("a", 1)
        self.assertEqual(board.get("a"), 1)
        self.assertEqual(board.get("missing"), None)
        self.assertEqual(board.get("missing", "fallback"), "fallback")
        self.assertEqual(board.require("a"), 1)
        with self.assertRaises(NodeError):
            board.require("missing")

    def test_update_keys_and_snapshot(self):
        board = Blackboard()
        board.update({"code.x": 1, "review.y": 2, "other": 3})
        self.assertEqual(board.keys(), ["code.x", "other", "review.y"])
        self.assertEqual(board.snapshot("code."), {"code.x": 1})
        self.assertEqual(sorted(board.snapshot()), ["code.x", "other", "review.y"])

    def test_namespace_helpers(self):
        board = Blackboard()
        board.ns_set("review", "issues", [1, 2])
        self.assertEqual(board.ns_get("review", "issues"), [1, 2])
        self.assertEqual(board.ns_get("review", "none", "d"), "d")
        board.ns_merge("review", {"counts": {"high": 1}, "verdict": "pass"})
        self.assertEqual(board.ns_all("review"),
                         {"issues": [1, 2], "counts": {"high": 1}, "verdict": "pass"})
        self.assertEqual(board.ns_all("")["review.verdict"], "pass")

    def test_artifacts(self):
        board = Blackboard()
        record = board.put_artifact("report.md", "# 报告", kind="doc", producer="documenter")
        self.assertEqual(record["name"], "report.md")
        self.assertEqual(record["kind"], "doc")
        self.assertEqual(record["producer"], "documenter")
        self.assertTrue(record["created_at"])
        self.assertEqual(board.artifact("report.md")["content"], "# 报告")
        self.assertIsNone(board.artifact("missing"))
        self.assertEqual(list(board.artifacts()), ["report.md"])
        board.put_artifact("report.md", "新内容")     # 同名覆盖
        self.assertEqual(board.artifact("report.md")["content"], "新内容")


class BlackboardFileTest(unittest.TestCase):
    """工作区文件读写：越界防护 + 指纹 + 列举。"""

    def test_write_read_hash_exists(self):
        with temp_workspace("board-files") as ws:
            board = Blackboard(workspace=ws)
            path = board.write_file("app/service.py", "print(1)\n")
            self.assertTrue(os.path.isabs(path))
            self.assertEqual(board.read_file("app/service.py"), "print(1)\n")
            self.assertTrue(board.file_exists("app/service.py"))
            self.assertFalse(board.file_exists("nope.py"))
            self.assertEqual(board.file_hash("app/service.py"),
                             board.file_hash("app/service.py"))
            board.write_file("app/service.py", "print(2)\n")
            self.assertNotEqual(board.file_hash("app/service.py"), "")
            self.assertEqual(board.read_file("nope.py"), None)
            self.assertEqual(board.read_file("nope.py", "默认"), "默认")
            self.assertEqual(board.file_hash("nope.py"), "")

    def test_list_files_sorted_and_pattern(self):
        with temp_workspace("board-list") as ws:
            board = Blackboard(workspace=ws)
            board.write_file("b.py", "b")
            board.write_file("a.py", "a")
            board.write_file("notes.md", "n")
            board.write_file(os.path.join("pkg", "c.py"), "c")
            board.write_file(os.path.join(".git", "hidden.py"), "x")
            self.assertEqual(board.list_files(), ["a.py", "b.py", "notes.md", "pkg/c.py"])
            self.assertEqual(board.list_files(pattern="*.py"), ["a.py", "b.py", "pkg/c.py"])
            self.assertIn("pkg/c.py", board.list_files(pattern="*.py"))

    def test_escape_is_rejected(self):
        with temp_workspace("board-escape") as ws:
            board = Blackboard(workspace=ws)
            outside = os.path.join(os.path.dirname(ws), "evil.txt")
            for action in (lambda: board.write_file("../evil.txt", "x"),
                           lambda: board.read_file("../evil.txt"),
                           lambda: board.file_hash("../evil.txt"),
                           lambda: board.file_exists("../evil.txt"),
                           lambda: board.write_file(outside, "x")):
                with self.subTest(action=action):
                    with self.assertRaises(PathSecurityError):
                        action()

    def test_reused_bus_is_exposed(self):
        bus = MessageBus()
        board = Blackboard(bus=bus)
        self.assertIs(board.bus, bus)
        self.assertIsInstance(Blackboard().bus, MessageBus)


class BlackboardRunStateTest(unittest.TestCase):
    """运行状态：记录、循环计数、指标卡。"""

    def _board(self, **kwargs):
        return Blackboard(**kwargs)

    def test_record_updates_turn_and_summary(self):
        board = self._board()
        board.state.status = "success"
        board.record(NodeRecord(node_id="n1", agent="coder", turn=1, status="ok", kind="result"))
        board.record(NodeRecord(node_id="n2", agent="reviewer", turn=3, status="failed",
                                kind="review", error="boom"))
        board.record(NodeRecord(node_id="n3", agent="fixer", turn=2, status="skipped"))
        self.assertEqual(board.state.turn, 3)          # 取最大回合
        self.assertEqual(len(board.state.records), 3)
        summary = board.summary()
        self.assertEqual(summary["nodes"], 3)
        self.assertEqual(summary["records"], 3)
        self.assertEqual(summary["turn"], 3)
        self.assertEqual(summary["turns"], 3)
        self.assertEqual((summary["ok"], summary["failed"], summary["skipped"]), (1, 1, 1))
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["artifacts"], 0)
        self.assertEqual(summary["keys"], 0)
        self.assertEqual(summary["messages"], 0)

    def test_iterations(self):
        board = self._board()
        self.assertEqual(board.iteration("impl"), 0)
        self.assertEqual(board.bump_iteration("impl"), 1)
        self.assertEqual(board.bump_iteration("impl"), 2)
        self.assertEqual(board.iteration("impl"), 2)
        self.assertEqual(board.summary()["iterations"], {"impl": 2})

    def test_run_id_and_bus_message_count(self):
        bus = MessageBus()
        board = Blackboard(bus=bus)
        self.assertTrue(board.run_id.startswith("run-"))
        bus.send("system", PERF_ID, BROADCAST, {"event": "turn_start"})
        self.assertEqual(board.summary()["messages"], 1)

    def test_to_dict_is_json_serializable(self):
        board = self._board()
        board.set("x", {"a": [1, 2]})
        board.put_artifact("d.md", "内容")
        board.record(NodeRecord(node_id="n1", turn=1))
        payload = board.to_dict()
        self.assertEqual(sorted(payload), ["artifacts", "kv", "metrics", "state"])
        json.dumps(payload, ensure_ascii=False)


class BlackboardPersistenceTest(unittest.TestCase):
    """save / load 往返一致。"""

    def test_save_load_roundtrip(self):
        with temp_workspace("board-save") as ws:
            board = Blackboard(workspace=ws)
            board.state.workflow = "code_review"
            board.state.status = "partial"
            board.set("issues", [{"id": "i1"}])
            board.ns_set("results", "coder", {"files": {"a.py": "x"}})
            board.put_artifact("run_report.md", "# R", kind="report", producer="report")
            board.write_file("app/service.py", "print(1)\n")
            board.record(NodeRecord(node_id="code", agent="coder", turn=1, status="ok",
                                    summary="生成代码", kind="result", messages=["msg-1"]))
            board.bump_iteration("impl")

            path = board.save(os.path.join(ws, "ckpt", "board.json"))
            self.assertTrue(os.path.isfile(path))
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["kv"]["issues"], [{"id": "i1"}])

            restored = Blackboard.load(path, workspace=ws)
            self.assertEqual(restored.get("issues"), [{"id": "i1"}])
            self.assertEqual(restored.ns_get("results", "coder"), {"files": {"a.py": "x"}})
            self.assertEqual(restored.artifact("run_report.md")["content"], "# R")
            self.assertEqual(restored.iteration("impl"), 1)
            self.assertEqual(restored.state.workflow, "code_review")
            self.assertEqual(restored.state.status, "partial")
            self.assertEqual(len(restored.state.records), 1)
            self.assertIsInstance(restored.state.records[0], NodeRecord)
            self.assertEqual(restored.state.records[0].messages, ["msg-1"])
            self.assertIn("app/service.py", restored.list_files())
            self.assertEqual(restored.to_dict()["metrics"], board.to_dict()["metrics"])

    def test_load_keeps_injected_bus(self):
        with temp_workspace("board-load-bus") as ws:
            board = Blackboard(workspace=ws)
            board.set("a", 1)
            path = board.save(os.path.join(ws, "board.json"))
            bus = MessageBus()
            restored = Blackboard.load(path, workspace=ws, bus=bus)
            self.assertIs(restored.bus, bus)

    def test_run_state_roundtrip(self):
        state = RunState(run_id="run-1", workflow="docs", status="success", turn=4,
                         iterations={"doc": 2}, started_at="2026-01-01T00:00:00",
                         finished_at="2026-01-01T00:00:05", duration=5.0,
                         records=[NodeRecord(node_id="n1", turn=4)], final={"verdict": "pass"},
                         error="")
        restored = RunState.from_dict(state.to_dict())
        self.assertEqual(restored.to_dict(), state.to_dict())


class NodeRecordTest(unittest.TestCase):
    """NodeRecord 的序列化容错。"""

    def test_roundtrip(self):
        record = NodeRecord(node_id="review", agent="reviewer", turn=2, status="ok",
                            started_at="2026-01-01T00:00:00", duration=1.234567,
                            summary="审查完成", kind="review", decision="pass",
                            result={"issues": []}, messages=["m1"], attempt=2)
        self.assertEqual(NodeRecord.from_dict(record.to_dict()).to_dict(), record.to_dict())

    def test_from_dict_tolerates_garbage(self):
        restored = NodeRecord.from_dict({"turn": "abc", "duration": None, "messages": "bad",
                                         "attempt": 0})
        self.assertEqual(restored.turn, 0)
        self.assertEqual(restored.duration, 0.0)
        self.assertEqual(restored.messages, [])
        self.assertEqual(restored.attempt, 1)


class DamagedCheckpointTest(unittest.TestCase):
    """``Blackboard.load`` 对损坏 / 旧版检查点的容错。"""

    def _write(self, ws: str, name: str, content: str) -> str:
        path = os.path.join(ws, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return path

    def test_empty_object_gives_empty_board(self):
        with temp_workspace("board-damaged-empty") as ws:
            board = Blackboard.load(self._write(ws, "empty.json", "{}"), workspace=ws)
        self.assertEqual(board.keys(), [])
        self.assertEqual(board.artifacts(), {})
        self.assertEqual(board.state.records, [])
        self.assertEqual(board.state.turn, 0)
        self.assertEqual(board.state.status, "pending")

    def test_non_object_checkpoint_raises_node_error(self):
        with temp_workspace("board-damaged-array") as ws:
            path = self._write(ws, "array.json", "[]")
            with self.assertRaises(NodeError):
                Blackboard.load(path, workspace=ws)

    def test_invalid_json_raises_node_error(self):
        with temp_workspace("board-damaged-json") as ws:
            path = self._write(ws, "broken.json", "这不是 JSON")
            with self.assertRaises(NodeError):
                Blackboard.load(path, workspace=ws)

    def test_damaged_fields_are_normalized(self):
        payload = {
            "kv": "不是字典",                       # 非法 kv → 空
            "artifacts": {"good": {"kind": "doc", "content": "x"}, "bad": "文本"},
            "state": {
                "run_id": "run-1",
                "turn": "不是数字",
                "iterations": {"L": "不是数字", "M": 2},
                "records": [{"node_id": "n1", "turn": 1}, None, "垃圾", 42],
            },
        }
        with temp_workspace("board-damaged-fields") as ws:
            path = self._write(ws, "partial.json", json.dumps(payload, ensure_ascii=False))
            board = Blackboard.load(path, workspace=ws)
        self.assertEqual(board.keys(), [])
        self.assertEqual(list(board.artifacts()), ["good"])       # 非 dict 产物被丢弃
        self.assertEqual(board.state.run_id, "run-1")
        self.assertEqual(board.state.turn, 0)
        self.assertEqual(board.state.iterations, {"L": 0, "M": 2})
        self.assertEqual(len(board.state.records), 1)             # 垃圾记录被过滤
        self.assertIsInstance(board.state.records[0], NodeRecord)
        self.assertEqual(board.state.records[0].node_id, "n1")

    def test_missing_state_key_uses_defaults(self):
        with temp_workspace("board-damaged-nostate") as ws:
            path = self._write(ws, "nostate.json", json.dumps({"kv": {"a": 1}}))
            board = Blackboard.load(path, workspace=ws)
        self.assertEqual(board.get("a"), 1)
        self.assertEqual(board.state.records, [])
        self.assertEqual(board.state.status, "pending")


if __name__ == "__main__":
    unittest.main()
