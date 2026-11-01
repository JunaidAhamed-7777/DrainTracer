import contextlib
import io
import json
import unittest
from unittest import mock

from src.agent_command import build_parser, main


class AgentClientTimeoutCliTests(unittest.TestCase):
    def test_request_timeout_is_independent_of_session_timeout(self):
        args = build_parser().parse_args(["--request-timeout", "2.5", "start", "--timeout", "37",
                                          "--target", "target.py"])
        self.assertEqual(args.timeout, 37)
        with mock.patch("src.agent_command.AgentClient") as client_type:
            client_type.return_value.request.return_value = {"started": True}
            self.assertEqual(main(["--request-timeout", "2.5", "start", "--timeout", "37",
                                   "--target", "target.py"]), 0)
        self.assertEqual(client_type.call_args.kwargs["timeout"], 2.5)

    def test_diagnostic_clients_honor_override_and_keep_one_second_default(self):
        for command in ("diagnose", "support"):
            for option, expected_timeout in ((["--request-timeout", "0.05"], 0.05), ([], 1.0)):
                with self.subTest(command=command, option=option):
                    output = io.StringIO()
                    diagnostics = {"ready": False}
                    with mock.patch("src.agent_command.AgentClient") as client_type, \
                            mock.patch("src.agent_diagnostics.inspect_agent", return_value=diagnostics), \
                            mock.patch("src.agent_diagnostics.support_reports", return_value=[]), \
                            mock.patch("src.agent_diagnostics.support_document", return_value={"ok": True}), \
                            contextlib.redirect_stdout(output):
                        result = main([*option, command])
                    self.assertEqual(result, 2 if command == "diagnose" else 0)
                    self.assertEqual(client_type.call_args.kwargs["timeout"], expected_timeout)


    def test_invalid_request_timeout_is_rejected_before_early_return_commands(self):
        commands = (
            ["serve"],
            ["diagnose"],
            ["recover", "session-id"],
        )
        for value in ("nan", "inf", "0", "-1"):
            for command in commands:
                with self.subTest(value=value, command=command):
                    output = io.StringIO()
                    with mock.patch("src.agent_command.build_parser") as parser_factory, \
                            mock.patch("src.agent_command.AgentClient") as client_type, \
                            mock.patch("src.agent_command.AgentServer") as server_type, \
                            mock.patch("src.agent_diagnostics.inspect_agent") as inspect_agent, \
                            contextlib.redirect_stdout(output):
                        parser_factory.side_effect = build_parser
                        result = main(["--request-timeout", value, *command])
                    self.assertEqual(result, 2)
                    self.assertFalse(client_type.called)
                    self.assertFalse(server_type.called)
                    self.assertFalse(inspect_agent.called)
                    self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_input")
