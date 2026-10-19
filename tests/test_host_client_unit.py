import unittest

from omo.host_client import (
    HostError,
    OpenResult,
    check_protocol,
    default_socket_path,
    parse_open_response,
)


class TestProtocol(unittest.TestCase):
    def test_default_socket_path(self):
        self.assertTrue(default_socket_path().endswith("/.omo/agent/rpc/rpc.sock"))

    def test_check_protocol_accepts_v1(self):
        check_protocol({
            "protocolVersion": 1,
            "capabilities": ["multi_session", "retain_on_disconnect"],
        })

    def test_check_protocol_rejects_wrong_version(self):
        with self.assertRaises(HostError) as ctx:
            check_protocol({"protocolVersion": 2, "capabilities": []})
        self.assertEqual(ctx.exception.code, "protocol_mismatch")

    def test_check_protocol_rejects_missing_capability(self):
        with self.assertRaises(HostError) as ctx:
            check_protocol({"protocolVersion": 1, "capabilities": ["multi_session"]})
        self.assertEqual(ctx.exception.code, "protocol_mismatch")

    def test_parse_open_response_uses_state_session_file(self):
        opened = parse_open_response({
            "success": True,
            "data": {
                "sessionId": "rpc-3",
                "state": {"sessionFile": "/tmp/session.jsonl", "sessionId": "durable-1",
                          "pendingQuestions": []},
            },
        })
        self.assertIsInstance(opened, OpenResult)
        self.assertEqual(opened.session_path, "/tmp/session.jsonl")
        self.assertEqual(opened.durable_session_id, "durable-1")
        self.assertEqual(opened.session_id, "rpc-3")

    def test_parse_open_response_falls_back_to_given_path(self):
        opened = parse_open_response({"success": True, "data": {"sessionId": "rpc-4"}},
                                     fallback_path="/tmp/old.jsonl")
        self.assertEqual(opened.session_path, "/tmp/old.jsonl")


if __name__ == "__main__":
    unittest.main()
