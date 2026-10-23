"""Release comparison and startup resilience without real network calls."""

from __future__ import annotations

import json
import time
import unittest
from http.client import IncompleteRead
from threading import Event
from unittest import mock
from urllib.error import HTTPError, URLError

from src import updates


def _response(payload: bytes) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = payload
    return response


class UpdateCheckTests(unittest.TestCase):
    def test_release_components_are_compared_numerically(self) -> None:
        cases = (
            ("v5.2.1", "5.2.2", True),
            ("v5.0.99", "5.0.100", True),
            ("v5.9.0", "5.10.0", True),
            ("v5.2.1", "6.0.0", True),
            ("v5.2.1", "5.2.1", False),
            ("v5.3.0", "5.2.1", False),
        )
        for current, latest, expected in cases:
            with self.subTest(current=current, latest=latest):
                payload = json.dumps({"info": {"version": latest, "yanked": False}}).encode()
                with mock.patch.object(updates, "urlopen", return_value=_response(payload)) as fetch:
                    self.assertIs(updates.check_for_update(current), expected)
                request = fetch.call_args.args[0]
                self.assertEqual(request.full_url, updates.PYPI_URL)
                self.assertEqual(request.get_header("Accept"), "application/json")
                self.assertEqual(fetch.call_args.kwargs["timeout"], updates.CHECK_TIMEOUT)

    def test_invalid_unpublished_and_yanked_metadata_remain_unknown(self) -> None:
        documents = (
            None, [], {}, {"info": None}, {"info": []}, {"info": {}},
            {"info": {"version": "6.0.0rc1"}},
            {"info": {"version": "6.0.0.dev1"}},
            {"info": {"version": "6.0.0+local"}},
            {"info": {"version": "6.0"}},
            {"info": {"version": 6}},
            {"info": {"version": "6.0.0", "yanked": True}},
        )
        for document in documents:
            with self.subTest(document=document):
                response = _response(json.dumps(document).encode())
                with mock.patch.object(updates, "urlopen", return_value=response):
                    self.assertIsNone(updates.check_for_update("v5.2.1"))

    def test_network_and_response_failures_do_not_interrupt_startup(self) -> None:
        failures = (
            URLError("offline"), TimeoutError(), OSError("TLS failure"),
            HTTPError(updates.PYPI_URL, 503, "unavailable", {}, None),
            IncompleteRead(b"partial"),
        )
        for failure in failures:
            with (
                self.subTest(failure=failure),
                mock.patch.object(updates, "urlopen", side_effect=failure),
            ):
                self.assertIsNone(updates.check_for_update("v5.2.1"))
        for payload in (b"not json", b"\xff", b" " * (updates._MAX_RESPONSE_BYTES + 1)):
            with (
                self.subTest(payload_size=len(payload)),
                mock.patch.object(updates, "urlopen", return_value=_response(payload)),
            ):
                self.assertIsNone(updates.check_for_update("v5.2.1"))

    def test_slow_dns_or_response_cannot_hold_up_startup(self) -> None:
        release = Event()
        completed = Event()

        def stalled_request(*_args, **_kwargs):
            try:
                release.wait(2)
                raise TimeoutError
            finally:
                completed.set()

        try:
            with mock.patch.object(updates, "urlopen", side_effect=stalled_request):
                started = time.monotonic()
                self.assertIsNone(updates.check_for_update("v5.2.1", timeout=0.02))
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            self.assertTrue(completed.wait(1))

    def test_invalid_current_version_does_not_contact_pypi(self) -> None:
        with mock.patch.object(updates, "urlopen") as fetch:
            self.assertIsNone(updates.check_for_update("invalid"))
        fetch.assert_not_called()

    def test_unavailable_worker_does_not_interrupt_startup(self) -> None:
        with mock.patch.object(updates, "Thread", side_effect=RuntimeError("no threads")):
            self.assertIsNone(updates.check_for_update("v5.2.1"))


if __name__ == "__main__":
    unittest.main()
