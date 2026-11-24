import json
import logging
import os
import time

import requests
from prometheus_client.parser import text_string_to_metric_families


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("incident-detector")

METRICS_URL = os.getenv(
    "DEMO_METRICS_URL",
    "http://demo:8000/metrics"
)
INCIDENT_API_URL = os.getenv(
    "INCIDENT_API_URL",
    "http://api:8000/incidents"
)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
SLOW_THRESHOLD = float(
    os.getenv("SLOW_REQUEST_THRESHOLD_SECONDS", "2")
)


def collect_metrics():
    response = requests.get(METRICS_URL, timeout=5)
    response.raise_for_status()

    snapshot = {
        "requests": {},
        "latency": {}
    }

    for family in text_string_to_metric_families(response.text):
        for sample in family.samples:
            labels = sample.labels

            if sample.name == "demo_requests_total":
                key = (
                    labels.get("endpoint", "unknown"),
                    labels.get("status", "unknown")
                )
                snapshot["requests"][key] = sample.value

            elif sample.name in (
                "demo_request_duration_seconds_sum",
                "demo_request_duration_seconds_count"
            ):
                endpoint = labels.get("endpoint", "unknown")

                if endpoint not in snapshot["latency"]:
                    snapshot["latency"][endpoint] = {
                        "sum": 0,
                        "count": 0
                    }

                if sample.name.endswith("_sum"):
                    snapshot["latency"][endpoint]["sum"] = sample.value
                else:
                    snapshot["latency"][endpoint]["count"] = sample.value

    return snapshot


def report_incident(incident_type, **details):
    incident = {
        "event": "incident_detected",
        "incident_type": incident_type,
        "detected_at": time.time(),
        **details
    }

    logger.warning(json.dumps(incident))

    payload = {
        "incident_type": incident_type,
        "service": "demo-service",
        "endpoint": details.get("endpoint"),
        "status_code": details.get("status_code"),
        "details": {
            **details,
            "source": "incident-detector",
            "detected_at": incident["detected_at"]
        }
    }

    try:
        response = requests.post(
            INCIDENT_API_URL,
            json=payload,
            timeout=5
        )
        response.raise_for_status()

        logger.info(
            "Incident successfully stored in API: %s",
            response.json().get("id")
        )

    except requests.RequestException:
        logger.exception("Failed to send incident to API")


def detect_changes(previous, current):
    for key, current_value in current["requests"].items():
        previous_value = previous["requests"].get(key, 0)
        increase = current_value - previous_value

        if increase <= 0:
            continue

        endpoint, status = key

        if status == "500":
            report_incident(
                "application_error",
                endpoint=endpoint,
                status_code=500,
                new_errors=increase
            )

        elif status == "503":
            report_incident(
                "dependency_error",
                endpoint=endpoint,
                status_code=503,
                new_errors=increase
            )

    for endpoint, current_data in current["latency"].items():
        previous_data = previous["latency"].get(
            endpoint,
            {"sum": 0, "count": 0}
        )

        new_count = current_data["count"] - previous_data["count"]
        new_sum = current_data["sum"] - previous_data["sum"]

        if new_count <= 0:
            continue

        average_duration = new_sum / new_count

        if average_duration > SLOW_THRESHOLD:
            report_incident(
                "slow_request",
                endpoint=endpoint,
                average_duration_seconds=round(average_duration, 3),
                threshold_seconds=SLOW_THRESHOLD,
                new_requests=new_count
            )


def main():
    logger.info("Incident detector started")
    logger.info("Monitoring metrics at %s", METRICS_URL)

    previous_snapshot = None

    while True:
        try:
            current_snapshot = collect_metrics()

            if previous_snapshot is None:
                previous_snapshot = current_snapshot
                logger.info("Initial metrics baseline recorded")

            else:
                detect_changes(previous_snapshot, current_snapshot)
                previous_snapshot = current_snapshot

        except requests.RequestException:
            logger.exception("Could not reach demo metrics endpoint")

        except Exception:
            logger.exception("Unexpected detector error")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
