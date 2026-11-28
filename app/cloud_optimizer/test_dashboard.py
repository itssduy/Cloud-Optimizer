from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings


@override_settings(DEBUG=True)
class DashboardTests(SimpleTestCase):
    def test_sample_page_and_filters(self):
        page = self.client.get("/preview/")
        self.assertContains(page, "Sample data")
        self.assertContains(page, "$4.82")
        self.assertContains(page, "old-staging-api")
        self.assertContains(page, "acme-old-logs")
        ec2 = self.client.get("/preview/?service=ec2")
        self.assertContains(ec2, "old-staging-api")
        self.assertNotContains(ec2, "acme-old-logs")
        s3 = self.client.get("/preview/?service=s3")
        self.assertContains(s3, "acme-old-logs")
        self.assertNotContains(s3, "old-staging-api")

    def test_preview_is_unavailable_in_production(self):
        with self.settings(DEBUG=False):
            self.assertEqual(self.client.get("/preview/").status_code, 404)

    def test_missing_data_is_not_shown_as_zero(self):
        with patch("cloud_optimizer.dashboard.read_example", return_value=None):
            page = self.client.get("/preview/")
        self.assertContains(page, "No scans yet")
        self.assertContains(page, '<p class="number">—</p>', count=4)
        self.assertNotContains(page, "$0.00")

    def test_scan_messages(self):
        examples = [
            ({"scan": {"status": "completed"}, "finding_count": 0}, "No open findings"),
            ({"scan": {"status": "partial"}}, "The latest scan is incomplete"),
            ({"scan": {"status": "failed"}}, "The latest scan failed"),
            ({"scan": {"status": "completed"}, "overdue": True}, "The daily scan is overdue"),
            ({"scan": {"status": "completed", "insufficient_data_count": 1}}, "had insufficient data"),
            ({"data_error": "Saved dashboard results are temporarily unavailable"}, "Saved dashboard results are temporarily unavailable"),
        ]
        for context, message in examples:
            with self.subTest(message=message):
                html = render_to_string("cloud_optimizer/dashboard.html", context)
                self.assertIn(message, html)
