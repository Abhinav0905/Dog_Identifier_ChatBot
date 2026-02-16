"""
Alerting service: sends structured alerts to console, webhook, or Slack
when distress threshold is met.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from config import SLACK_WEBHOOK_URL, ALERT_WEBHOOK_URL
import database as db

logger = logging.getLogger("dharmasala.alerts")


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


def send_alert(incident_id: str, triage_result: dict, location: Optional[dict] = None, similar_id: Optional[str] = None) -> str:
    """Dispatch alert to configured channels. Returns alert_id."""
    payload = build_alert_payload(incident_id, triage_result, location, similar_id)

    # Always log to console
    _log_alert(payload)

    # Try Slack webhook
    channel = "console"
    if SLACK_WEBHOOK_URL:
        try:
            _send_slack(payload)
            channel = "slack"
        except Exception as e:
            logger.error(f"Slack alert failed: {e}")

    # Try generic webhook
    if ALERT_WEBHOOK_URL:
        try:
            _send_webhook(payload)
            channel = "webhook"
        except Exception as e:
            logger.error(f"Webhook alert failed: {e}")

    # Persist alert record
    reason = f"Severity {triage_result.get('severity_score', 0)}/10 exceeds threshold"
    alert_id = db.create_alert(incident_id, channel, reason)

    # Update incident status
    db.update_incident(incident_id, status="alerted")

    return alert_id


def _log_alert(payload: dict):
    """Log alert to console with clear formatting."""
    logger.warning(
        "\n"
        "====================================================\n"
        "  DHARMASALA RESCUE ALERT - %s SEVERITY\n"
        "====================================================\n"
        "  Incident:   %s\n"
        "  Severity:   %s (%s/10) | Confidence: %.0f%%\n"
        "  Indicators: %s\n"
        "  Location:   %s\n"
        "  Time:       %s\n"
        "====================================================",
        payload["severity"].upper(),
        payload["incident_id"][:12] + "...",
        payload["severity"],
        payload["severity_score"],
        payload["confidence"] * 100,
        ", ".join(payload["distress_indicators"][:3]) or "N/A",
        _format_location(payload.get("location")),
        payload["timestamp"],
    )


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
    urllib.request.urlopen(req, timeout=10)


def _send_webhook(payload: dict):
    """Send alert to generic webhook endpoint."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(ALERT_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
