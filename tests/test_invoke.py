"""CLI behavior for native provider events, using no AWS or network calls."""

from contextlib import redirect_stderr, redirect_stdout
from http.client import BadStatusLine, IncompleteRead, LineTooLong
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from examples.offline_demo import response, streaming_response, event_frame
from gateway_fallback import FallbackResult, HttpResponse
import invoke


class FlushedOutput(StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        super().flush()


class CliTests(unittest.TestCase):
    def run_cli(self, reply):
        config = {
            "GATEWAY_ENDPOINT": "https://example.invalid/inference",
            "PRIMARY_MODEL_ID": "high",
            "FALLBACK_MODEL_ID": "standard",
            "GATEWAY_BEARER_TOKEN": "test-only-token",
        }
        request_path = Path(__file__).resolve().parents[1] / "examples/request.json"
        result = FallbackResult("high", ("high",), reply, False, None)
        output, errors = FlushedOutput(), StringIO()
        with (
            patch.dict(invoke.os.environ, config, clear=True),
            patch("sys.argv", ["invoke.py", "--request", str(request_path)]),
            patch.object(invoke, "GatewayClient") as client,
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            if isinstance(reply, Exception):
                client.return_value.invoke.side_effect = reply
            else:
                client.return_value.invoke.return_value = result
            status = invoke.main()
            client.return_value.invoke.assert_called_once()
            args, kwargs = client.return_value.invoke.call_args
            self.assertNotIn("model", args[0])
            self.assertNotIn("stream", args[0])
            self.assertEqual(kwargs["model"], "high")
        return status, output, errors

    def test_complete_stream_flushes_events_and_reports_native_model(self):
        reply = streaming_response("served-model")
        status, output, errors = self.run_cli(reply)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(events[0]["type"], "gateway_response")
        self.assertEqual([e["type"] for e in events[1:-1]], ["model_event"] * 3)
        self.assertEqual(
            events[-1], {"type": "complete", "response_reported_model": "served-model"}
        )
        self.assertGreaterEqual(output.flush_count, len(events))
        self.assertTrue(reply.stream.closed)

    def test_clean_frame_eof_without_native_message_stop_is_failure(self):
        from io import BytesIO
        reply = HttpResponse(
            200,
            headers={"content-type": "application/vnd.amazon.eventstream"},
            stream=BytesIO(event_frame({"type": "content_block_delta", "delta": {}})),
        )
        status, output, errors = self.run_cli(reply)
        self.assertEqual(status, 2)
        self.assertIn('"model_event"', output.getvalue())
        self.assertNotIn('"complete"', output.getvalue())
        self.assertEqual(json.loads(errors.getvalue())["error"], "StreamError")
        self.assertTrue(reply.stream.closed)

    def test_native_error_after_partial_output_never_reports_completion(self):
        from io import BytesIO
        frames = event_frame({"type": "content_block_delta", "delta": {"text": "part"}})
        frames += event_frame({"type": "error", "error": {"type": "overloaded_error"}})
        reply = HttpResponse(
            200, headers={"content-type": "application/vnd.amazon.eventstream"},
            stream=BytesIO(frames),
        )
        status, output, errors = self.run_cli(reply)
        self.assertEqual(status, 2)
        self.assertIn('"part"', output.getvalue())
        self.assertNotIn('"complete"', output.getvalue())
        self.assertEqual(json.loads(errors.getvalue())["error"], "StreamError")
        self.assertTrue(reply.stream.closed)

    def test_http_error_is_reported_without_trying_to_parse_a_stream(self):
        status, output, errors = self.run_cli(response(403, {"error": "AccessDenied"}))
        self.assertEqual(status, 1)
        self.assertEqual(errors.getvalue(), "")
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]["type"], "http_error")
        self.assertNotIn('"complete"', output.getvalue())

    def test_http_protocol_errors_return_exit_two_without_leaking_partial_body(self):
        for failure in (
            IncompleteRead(b"private-partial-response", 10),
            BadStatusLine("private-status-line"),
            LineTooLong("private-header"),
        ):
            with self.subTest(error=type(failure).__name__):
                status, output, errors = self.run_cli(failure)
                self.assertEqual(status, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(json.loads(errors.getvalue())["type"], "client_error")
                self.assertNotIn("private-", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
