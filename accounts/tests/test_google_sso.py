"""Google SSO configuration, UI, and OAuth handshake regression tests."""

from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

# A harmless test-only OAuth client; network requests to Google are not made.
GOOGLE_TEST_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": "test-client.apps.googleusercontent.com",
            "secret": "not-a-real-secret",
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "OAUTH_PKCE_ENABLED": True,
        "EMAIL_AUTHENTICATION": True,
        "EMAIL_AUTHENTICATION_AUTO_CONNECT": True,
    }
}


class GoogleSSOTests(TestCase):
    """Do not break password pages while adding an opt-in Google flow."""

    @override_settings(GOOGLE_OAUTH_ENABLED=False)
    def test_no_google_buttons_without_credentials(self):
        """The existing password flows work with Google OAuth unconfigured."""
        for page in ("login", "register"):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "with Google")

    @override_settings(
        GOOGLE_OAUTH_ENABLED=True, SOCIALACCOUNT_PROVIDERS=GOOGLE_TEST_PROVIDERS
    )
    def test_login_and_register_render_google_post_forms(self):
        """Both pages provide CSRF-protected forms to the Google login URL."""
        for page in ("login", "register"):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'action="/accounts/google/login/"')
                self.assertContains(response, 'name="csrfmiddlewaretoken"')
                self.assertContains(response, "Google")

    @override_settings(
        GOOGLE_OAUTH_ENABLED=True, SOCIALACCOUNT_PROVIDERS=GOOGLE_TEST_PROVIDERS
    )
    def test_oauth_start_requires_csrf_and_uses_pkce(self):
        """Starting the flow POSTs to allauth and redirects to Google securely."""
        client = Client(enforce_csrf_checks=True)
        login_response = client.get(reverse("login"))
        csrf_token = login_response.cookies["csrftoken"].value
        google_login = reverse("google_login")

        self.assertEqual(client.post(google_login).status_code, 403)

        response = client.post(google_login, HTTP_X_CSRFTOKEN=csrf_token)
        self.assertEqual(response.status_code, 302)

        location = urlparse(response["Location"])
        params = parse_qs(location.query)
        self.assertEqual(location.scheme, "https")
        self.assertEqual(location.netloc, "accounts.google.com")
        expected_client_id = "test-client.apps.googleusercontent.com"
        self.assertEqual(params["client_id"], [expected_client_id])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertTrue(params["code_challenge"][0])
        self.assertTrue(params["state"][0])
        callback_url = "http://testserver/accounts/google/login/callback/"
        self.assertEqual(params["redirect_uri"], [callback_url])

    def test_google_token_storage_disabled(self):
        """No Google access/refresh tokens should be persisted in our database."""
        self.assertFalse(settings.SOCIALACCOUNT_STORE_TOKENS)
        provider_config = settings.SOCIALACCOUNT_PROVIDERS["google"]
        self.assertTrue(provider_config["OAUTH_PKCE_ENABLED"])
