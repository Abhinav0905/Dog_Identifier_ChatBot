import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app
from models import ChatResponse
from services import twilio_whatsapp


class TestTwilioWhatsAppHelpers(unittest.TestCase):
    def test_sender_is_pseudonymized_and_stable(self):
        first = twilio_whatsapp.session_id_for_sender("whatsapp:+15551234567")
        second = twilio_whatsapp.session_id_for_sender("whatsapp:+15551234567")

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("whatsapp:"))
        self.assertNotIn("+15551234567", first)

    def test_twiml_reply_is_whatsapp_friendly(self):
        xml = twilio_whatsapp.build_twiml("**Dog bite safety**\n\nWash the wound.")

        self.assertIn("<Response>", xml)
        self.assertIn("<Message>", xml)
        self.assertIn("*Dog bite safety*", xml)

    def test_long_whatsapp_reply_keeps_helpful_links(self):
        long_text = (
            "Assessment details. " * 120
            + "\n\nHelpful links:\n"
            + "- Contact Dharamsala Animal Rescue: https://dharamsalaanimalrescue.org/contact/"
        )

        xml = twilio_whatsapp.build_twiml(long_text)

        self.assertIn("Reply with a follow-up question", xml)
        self.assertIn("Helpful links:", xml)
        self.assertIn("https://dharamsalaanimalrescue.org/contact/", xml)

    def test_signature_validation_accepts_valid_and_rejects_invalid(self):
        from twilio.request_validator import RequestValidator

        url = "https://askdorjee.dharamsalaanimalrescue.org/v1/integrations/twilio/whatsapp"
        params = {"From": "whatsapp:+15551234567", "Body": "Help"}
        token = "unit-test-auth-token"
        valid_signature = RequestValidator(token).compute_signature(url, params)
        with patch.object(twilio_whatsapp.config, "TWILIO_VALIDATE_SIGNATURES", True), \
             patch.object(twilio_whatsapp.config, "TWILIO_AUTH_TOKEN", token):
            self.assertTrue(
                twilio_whatsapp.validate_webhook(url, params, valid_signature)
            )
            self.assertFalse(
                twilio_whatsapp.validate_webhook(url, params, "invalid-signature")
            )


class TestTwilioWhatsAppWebhook(unittest.TestCase):
    def setUp(self):
        self.query_router_client_patcher = patch.object(app.query_router, "client", None)
        self.query_router_client_patcher.start()
        self.signature_patcher = patch.object(
            app.config,
            "TWILIO_VALIDATE_SIGNATURES",
            False,
        )
        self.signature_patcher.start()
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.signature_patcher.stop()
        self.query_router_client_patcher.stop()

    @patch("app.db.save_session_location")
    def test_location_pin_is_saved_and_acknowledged(self, save_location):
        response = self.client.post(
            "/v1/integrations/twilio/whatsapp",
            data={
                "From": "whatsapp:+15551234567",
                "To": "whatsapp:+14155238886",
                "MessageSid": "SM-location",
                "Body": "",
                "NumMedia": "0",
                "Latitude": "32.2196",
                "Longitude": "76.3234",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Location received and saved", response.text)
        save_location.assert_called_once()

    @patch("app.db.get_session_location", return_value=None)
    @patch("app.twilio_whatsapp.send_whatsapp_message", return_value="SM-text-outbound")
    @patch(
        "app.chat_query",
        return_value=ChatResponse(response="Wash the bite with soap and running water."),
    )
    def test_text_question_is_acknowledged_then_answered_in_background(
        self,
        chat_query,
        send_whatsapp_message,
        _get_location,
    ):
        response = self.client.post(
            "/v1/integrations/twilio/whatsapp",
            data={
                "From": "whatsapp:+15551234567",
                "To": "whatsapp:+14155238886",
                "MessageSid": "SM-text",
                "Body": "A dog bit me, what should I do?",
                "NumMedia": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/xml")
        self.assertIn("Message received", response.text)
        chat_query.assert_called_once()
        send_whatsapp_message.assert_called_once_with(
            to="whatsapp:+15551234567",
            from_="whatsapp:+14155238886",
            text="Wash the bite with soap and running water.",
        )

    @patch("app.config.WHATSAPP_DEMO_LOCATION_FALLBACK", True)
    @patch("app.config.WHATSAPP_DEMO_LAT", 32.2196)
    @patch("app.config.WHATSAPP_DEMO_LNG", 76.3234)
    @patch("app.db.get_session_location", return_value=None)
    @patch("app.twilio_whatsapp.download_image_media", return_value=(b"jpeg-bytes", "image/jpeg", "dog.jpg"))
    @patch("app.twilio_whatsapp.send_whatsapp_message", return_value="SM-outbound")
    @patch("app.triage_image", new_callable=AsyncMock)
    def test_media_without_location_uses_demo_fallback(
        self,
        triage_image,
        send_whatsapp_message,
        _download_image,
        _get_location,
    ):
        dar_phone = app.config.DAR_PHONE_NUMBER or "+91 98828 58631"
        triage_image.return_value = ChatResponse(
            response="Photo processed",
            resource_links=[
                {
                    "label": "Contact Dharamsala Animal Rescue",
                    "url": app.config.DAR_CONTACT_URL,
                    "phone": dar_phone,
                },
                {
                    "label": "Find nearby vets",
                    "url": "https://www.google.com/maps/search/?api=1&query=veterinarian+near+32.2196%2C76.3234",
                },
            ],
        )

        response = self.client.post(
            "/v1/integrations/twilio/whatsapp",
            data={
                "From": "whatsapp:+15551234567",
                "To": "whatsapp:+14155238886",
                "MessageSid": "SM-media",
                "Body": "",
                "NumMedia": "1",
                "MediaContentType0": "image/jpeg",
                "MediaUrl0": "https://api.twilio.com/fake-media",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Photo received", response.text)
        kwargs = triage_image.await_args.kwargs
        self.assertEqual(kwargs["lat"], 32.2196)
        self.assertEqual(kwargs["lng"], 76.3234)
        self.assertEqual(kwargs["location_source"], "whatsapp_demo")
        send_whatsapp_message.assert_called_once_with(
            to="whatsapp:+15551234567",
            from_="whatsapp:+14155238886",
            text=(
                "Photo processed\n\n"
                "Helpful links:\n"
                f"- Contact Dharamsala Animal Rescue: {app.config.DAR_CONTACT_URL}"
                f" | Phone: {dar_phone}\n"
                "- Find nearby vets: "
                "https://www.google.com/maps/search/?api=1&query=veterinarian+near+32.2196%2C76.3234"
            ),
        )


if __name__ == "__main__":
    unittest.main()
