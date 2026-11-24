import unittest

from fastapi.testclient import TestClient
from apps.demo.main import app


class TestDemoRegression(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, raise_server_exceptions=False)

    def test_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")

    def test_normal_request(self):
        response = self.client.get("/demo/ok")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

    def test_intentional_crash_simulation(self):
        response = self.client.get("/demo/crash")
        self.assertEqual(response.status_code, 500)

    def test_intentional_database_simulation(self):
        response = self.client.get("/demo/database")
        self.assertEqual(response.status_code, 503)

    def test_slow_endpoint(self):
        response = self.client.get("/demo/slow?seconds=1")
        self.assertEqual(response.status_code, 200)

    def test_metrics_endpoint(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("demo_requests_total", response.text)


if __name__ == "__main__":
    unittest.main()
