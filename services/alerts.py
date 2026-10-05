"""
Alerting service: sends structured alerts to console, webhook, or Slack
when distress threshold is met.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from config import SLACK_WEBHOOK_URL, ALERT_WEBHOOK_URL
import database as db

logger = logging.getLogger("dharmasala.alerts")


@dataclass(frozen=True)
class AlertDeliveryResult:
    alert_id: str
    status: str
    delivered: bool
    channels: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)


def build_alert_payload(incident_id: str, triage_result: dict, location: Optional[dict] = None, similar_id: Optional[str] = None) -> dict:
    """Build structured alert payload per the HLD spec."""
    return {
        "incident_id": incident_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": triage_result.get("severity", "unknown"),
        "severity_score": triage_result.get("severity_score", 0),
        "confidence": triage_result.get("confidence", 0.0),
        "distress_indicators": triage_result.get("indicators", []),
        "location": location,
        "similar_incident_reference": similar_id,
        "admin_console_url": f"/admin.html#incident/{incident_id}",
    }


def send_alert(incident_id: str, triage_result: dict, location: Optional[dict] = None, similar_id: Optional[str] = None, *, force_retry: bool = False) -> AlertDeliveryResult:
    """Record an attempt and report whether an external channel accepted it.

    A logged incident is not a delivered notification. Channel acceptance also
    does not mean a rescue team acknowledged the case or promised a pickup.
    """
    payload = build_alert_payload(incident_id, triage_result, location, similar_id)
    reason = f"Severity {triage_result.get('severity_score', 0)}/10 exceeds threshold"
    reserved = db.reserve_alert_delivery(incident_id, payload, reason, force_retry=force_retry)
    if not reserved["acquired"]:
        try:
            details = json.loads(reserved.get("delivery_details") or "{}")
        except (TypeError, ValueError):
            details = {}
        return AlertDeliveryResult(
            reserved["alert_id"], reserved["delivery_status"], reserved["delivery_status"] == "delivered",
            details.get("channels", []), details.get("failures", {}),
        )
    return _deliver_reserved_alert(reserved)


def _deliver_reserved_alert(reserved: dict) -> AlertDeliveryResult:
    alert_id, incident_id = reserved["alert_id"], reserved["incident_id"]
    payload = json.loads(reserved["delivery_payload"])
    payload["alert_id"] = alert_id
    _log_alert(payload)
    channels = []
    failures = {}
    for name, configured, send in (
        ("slack", bool(SLACK_WEBHOOK_URL), _send_slack),
        ("webhook", bool(ALERT_WEBHOOK_URL), _send_webhook),
    ):
        if not configured:
            continue
        try:
            send(payload)
            channels.append(name)
        except Exception as exc:
            # Exception strings may contain private webhook URLs or credentials.
            failures[name] = type(exc).__name__
            logger.error("alert_send_failed alert_id=%s incident_id=%s channel=%s error_type=%s",
                         alert_id, incident_id, name, type(exc).__name__)
    status = "delivered" if channels else "failed" if failures else "not_configured"
    recorded = db.finish_alert_delivery(
        alert_id, reserved["delivery_lease_token"], status, channels=channels, failures=failures,
    )
    logger.info("alert_delivery alert_id=%s incident_id=%s status=%s",
                alert_id, incident_id, status if recorded else "pending")
    if channels and recorded:
        db.update_incident(incident_id, status="alerted")
    return AlertDeliveryResult(alert_id, status if recorded else "pending", bool(channels) and recorded, channels, failures)


def retry_failed_alerts(limit: int = 10) -> dict[str, int]:
    """Retry due web alerts at most three times using persisted, fenced claims."""
    result = {"attempted": 0, "delivered": 0, "failed": 0}
    if not (SLACK_WEBHOOK_URL or ALERT_WEBHOOK_URL):
        return result
    for reserved in db.claim_failed_alerts(limit):
        result["attempted"] += 1
        try:
            delivery = _deliver_reserved_alert(reserved)
            result["delivered" if delivery.delivered else "failed"] += 1
        except Exception as exc:
            db.finish_alert_delivery(
                reserved["alert_id"], reserved["delivery_lease_token"], "failed",
                channels=[], failures={"worker": type(exc).__name__},
            )
            result["failed"] += 1
            logger.error("alert_retry_failed alert_id=%s incident_id=%s error_type=%s",
                         reserved["alert_id"], reserved["incident_id"], type(exc).__name__)
    return result


def acknowledge_alert(alert_id: str, actor: str) -> bool:
    """Call only after authenticating the receiving operator at the API boundary."""
    return db.acknowledge_delivered_alert(alert_id, actor)


def _log_alert(payload: dict):
    """Keep operational IDs without copying location or clinical data to logs."""
    logger.info("alert_attempt alert_id=%s incident_id=%s status=pending",
                payload.get("alert_id", ""), payload["incident_id"])


def _format_location(loc: Optional[dict]) -> str:
    if not loc:
        return "Not available"
    return f"{loc.get('lat', '?')}, {loc.get('lng', '?')} (source: {loc.get('source', 'unknown')})"


def _send_slack(payload: dict):
    """Send alert to Slack via webhook. In production, use httpx/aiohttp."""
    import urllib.request
    slack_message = {
        "text": f":rotating_light: *Rescue Alert - {payload['severity'].upper()}*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Rescue Alert - {payload['severity'].upper()} Priority"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Incident:*\n`{payload['incident_id'][:12]}...`"},
                    {"type": "mrkdwn", "text": f"*Severity:*\n{payload['severity_score']}/10 ({payload['confidence']:.0%} confidence)"},
                    {"type": "mrkdwn", "text": f"*Indicators:*\n{', '.join(payload['distress_indicators'][:3])}"},
                    {"type": "mrkdwn", "text": f"*Location:*\n{_format_location(payload.get('location'))}"},
                ],
            },
        ],
    }
    data = json.dumps(slack_message).encode("utf-8")
    req = urllib.request.Request(SLACK_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("Slack did not accept the alert")


def _send_webhook(payload: dict):
    """Send alert to generic webhook endpoint."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(ALERT_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("Webhook did not accept the alert")
