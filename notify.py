"""Booking e-mails. AWS mode -> Amazon SNS. Local mode -> saved to the local 'inbox'."""
import json
import logging
import os
from datetime import datetime

import db

log = logging.getLogger("moviemagic.notify")
_sns = None
_topic = os.getenv("SNS_TOPIC_ARN", "")


def _client():
    global _sns, _topic
    if _sns is None:
        import boto3
        _sns = boto3.client("sns", region_name=db.REGION)
        if not _topic:
            _topic = _sns.create_topic(Name="MovieMagicNotifications")["TopicArn"]  # idempotent
    return _sns, _topic


def subscribe_user(email):
    """Subscribe the user's e-mail to the topic. Filter policy = they only get their own tickets."""
    if not db.USE_AWS:
        return {"ok": True, "note": "Local mode: e-mails are previewed in the app."}
    try:
        sns, topic = _client()
        sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=email,
                      Attributes={"FilterPolicy": json.dumps({"email": [email]})})
        return {"ok": True, "note": "Check your inbox and confirm the AWS notification subscription to receive tickets."}
    except Exception as e:  # never block registration on SNS problems
        log.exception("SNS subscribe failed")
        return {"ok": False, "note": f"Could not subscribe to notifications: {e}"}


def booking_message(b):
    lines = [
        f"Hi {b['name']},", "",
        "Your Movie Magic booking is confirmed!", "",
        f"Booking ID : {b['booking_id']}",
        f"Movie/Event: {b['title']}",
        f"Venue      : {b['theatre']}, {b['city']}",
        f"Date & time: {b['date']} at {b['time']}",
        f"Seats      : {', '.join(b['seats'])}",
        f"Amount paid: Rs. {b['total']}", "",
        "Show this e-mail or your booking ID at the counter. Enjoy the show!",
        "- Team Movie Magic",
    ]
    return f"Movie Magic ticket: {b['title']} ({b['booking_id']})", "\n".join(lines)


def send_booking_email(b, store):
    subject, body = booking_message(b)
    if not db.USE_AWS:
        store.add_notification({"booking_id": b["booking_id"], "to": b["email"], "subject": subject,
                                "body": body, "sent_at": datetime.utcnow().isoformat()})
        log.info("[local e-mail] to=%s subject=%s", b["email"], subject)
        return {"ok": True, "channel": "local preview"}
    try:
        sns, topic = _client()
        sns.publish(TopicArn=topic, Subject=subject[:100], Message=body,
                    MessageAttributes={"email": {"DataType": "String", "StringValue": b["email"]}})
        return {"ok": True, "channel": "Amazon SNS"}
    except Exception as e:
        log.exception("SNS publish failed")
        return {"ok": False, "channel": "Amazon SNS", "error": str(e)}
