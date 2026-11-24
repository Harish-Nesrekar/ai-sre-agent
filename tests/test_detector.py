import unittest
from unittest.mock import Mock, patch

from apps.detector import main as detector


class DetectorRetryTests(unittest.TestCase):

    def setUp(self):
        detector.pending_incidents.clear()

    def tearDown(self):
        detector.pending_incidents.clear()

    @patch("apps.detector.main.requests.post")
    def test_successful_delivery_removes_incident(self, mock_post):
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {"id": "incident-123"}
        )
        mock_post.return_value.raise_for_status.return_value = None

        detector.report_incident(
            "application_error",
            endpoint="/demo/ok",
            status_code=500
        )

        self.assertEqual(len(detector.pending_incidents), 1)

        detector.deliver_pending_incidents()

        self.assertEqual(len(detector.pending_incidents), 0)
        mock_post.assert_called_once()

    @patch("apps.detector.main.requests.post")
    def test_failed_delivery_keeps_incident_queued(self, mock_post):
        mock_post.side_effect = detector.requests.ConnectionError(
            "API unavailable"
        )

        detector.report_incident(
            "application_error",
            endpoint="/demo/ok",
            status_code=500
        )

        detector.deliver_pending_incidents()

        self.assertEqual(len(detector.pending_incidents), 1)
        mock_post.assert_called_once()

    @patch("apps.detector.main.requests.post")
    def test_failed_delivery_succeeds_on_retry(self, mock_post):
        failed_response = detector.requests.ConnectionError(
            "API temporarily unavailable"
        )
        successful_response = Mock(
            status_code=200,
            json=lambda: {"id": "incident-456"}
        )
        successful_response.raise_for_status.return_value = None

        mock_post.side_effect = [
            failed_response,
            successful_response
        ]

        detector.report_incident(
            "application_error",
            endpoint="/demo/ok",
            status_code=500
        )

        detector.deliver_pending_incidents()
        self.assertEqual(len(detector.pending_incidents), 1)

        detector.deliver_pending_incidents()
        self.assertEqual(len(detector.pending_incidents), 0)

        self.assertEqual(mock_post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
