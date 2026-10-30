"""Signing in to Google, shared by every Google platform (Classroom today).

Google's APIs take an OAuth login, not a password. BananaGent is the "app" in
that login: a Google Cloud project with a Desktop-app OAuth client, whose ID
and secret are in secrets.env as GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
(see the README for making one). Connecting opens Google's sign-in page in the
default browser on a loopback address; the user picks an account and approves
the scopes, and the page hands the login back to a one-shot local server.

Each platform keeps its own login (a GoogleLogin per platform, a token file
each), so Classroom can be the school account while something else is a
personal one. The refresh token in that file is what keeps it working across
restarts; access tokens are refreshed from it as they expire.
"""

import json
import os
import threading

import config
from . import NotConnected

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REVOKE = "https://oauth2.googleapis.com/revoke"
# Google answers some scopes under another name than the one asked for — the
# same permission, renamed. oauthlib treats any difference as a failed login
# unless told not to, and the granted-scope check has to read them as equal.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
_AUTH = "https://www.googleapis.com/auth/"
_SAME_SCOPE = {
    _AUTH + "classroom.student-submissions.me.readonly": _AUTH + "classroom.coursework.me.readonly",
    _AUTH + "classroom.student-submissions.students.readonly":
        _AUTH + "classroom.coursework.students.readonly",
}


def _client_config():
    keys = {}
    try:
        with open(os.path.join(APP, config.SECRETS_FILE), encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key.strip() in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
                    keys[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    if not keys.get("GOOGLE_CLIENT_ID") or not keys.get("GOOGLE_CLIENT_SECRET"):
        raise NotConnected(
            "No Google sign-in client is set up. The user has to make a Desktop-app "
            "OAuth client in Google Cloud and put GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in secrets.env (see the README).")
    return {"installed": {
        "client_id": keys["GOOGLE_CLIENT_ID"],
        "client_secret": keys["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


class GoogleLogin:
    def __init__(self, name, title, scopes):
        self.name = name
        self.title = title
        self.scopes = list(scopes)
        self.path = os.path.join(APP, config.INTEGRATION_TOKENS_DIR, f"{name}.json")
        self._lock = threading.Lock()
        self._creds = None

    # -- the login ------------------------------------------------------------
    def connected(self):
        return os.path.exists(self.path)

    def connect(self):
        """Open Google's sign-in page and wait for the user to approve."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_config(_client_config(), self.scopes)
        try:
            creds = flow.run_local_server(
                port=0, open_browser=True,
                timeout_seconds=config.INTEGRATION_CONNECT_SECONDS,
                authorization_prompt_message="",
                success_message=(f"BananaGent is connected to {self.title}. "
                                 "You can close this tab."),
                # A refresh token every time, so the login outlives the hour an
                # access token lasts — even on a second connect.
                access_type="offline", prompt="consent")
        except Exception as e:
            if "Timeout" in type(e).__name__:
                raise RuntimeError(
                    f"Signing in to {self.title} didn't finish within "
                    f"{config.INTEGRATION_CONNECT_SECONDS} seconds. Try again.") from None
            raise RuntimeError(f"Signing in to {self.title} failed "
                               f"({type(e).__name__}: {e}). Try again.") from None
        if not creds.refresh_token:
            raise RuntimeError(f"Google signed in to {self.title} but gave no refresh "
                               "token, so it would stop working within the hour. Try again.")
        with self._lock:
            self._save(creds)
        granted = {_SAME_SCOPE.get(s, s) for s in (creds.granted_scopes or self.scopes)}
        missing = [s.rsplit("/", 1)[-1] for s in self.scopes if s not in granted]
        return missing

    def disconnect(self):
        """Revoke the login with Google and forget it here."""
        with self._lock:
            creds, self._creds = self._load(), None
            if os.path.exists(self.path):
                os.remove(self.path)
        if creds is None:
            return False
        try:
            from google.auth.transport.requests import Request
            Request()(_REVOKE, method="POST", body=f"token={creds.refresh_token}",
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        except Exception:
            pass  # forgotten here either way; Google drops unused tokens itself
        return True

    def credentials(self):
        """Working credentials, refreshed if they've expired."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        with self._lock:
            creds = self._creds or self._load()
            if creds is None:
                raise NotConnected(
                    f"{self.title} isn't connected. Ask the user, then call "
                    f"integration_connect with platform '{self.name}' — it opens a "
                    "Google sign-in page for them.")
            if not creds.valid:
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    raise NotConnected(
                        f"The {self.title} login stopped working ({e}). It was revoked, "
                        "or the Google Cloud app is in Testing mode, where logins last "
                        "7 days. Call integration_connect with platform "
                        f"'{self.name}' to sign in again.") from None
                self._save(creds)
            self._creds = creds
            return creds

    def _load(self):
        from google.oauth2.credentials import Credentials
        try:
            with open(self.path, encoding="utf-8") as f:
                return Credentials.from_authorized_user_info(json.load(f), self.scopes)
        except (OSError, ValueError):
            return None

    def _save(self, creds):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        self._creds = creds

    # -- calling the API ----------------------------------------------------------
    def get(self, url, **params):
        """GET one JSON resource. Errors come back as sentences the model can act on."""
        from google.auth.transport.requests import AuthorizedSession

        session = AuthorizedSession(self.credentials())
        params = {k: v for k, v in params.items() if v is not None}
        r = session.get(url, params=params, timeout=config.INTEGRATION_TIMEOUT)
        if r.ok:
            return r.json()
        try:
            err = r.json().get("error", {})
            message, status = err.get("message", r.text), err.get("status", "")
        except ValueError:
            message, status = r.text[:300], ""
        if r.status_code == 401:
            raise NotConnected(f"Google turned down the {self.title} login ({message}). "
                               f"Call integration_connect with platform '{self.name}'.")
        if "has not been used in project" in message or "SERVICE_DISABLED" in r.text:
            raise RuntimeError(
                f"The {self.title} API is switched off in BananaGent's Google Cloud "
                f"project. The user has to enable it there: {message}")
        if "insufficient" in message.lower() and "scope" in message.lower():
            raise NotConnected(
                f"The {self.title} login doesn't cover this ({message}). Call "
                f"integration_connect with platform '{self.name}' and have the user "
                "tick every box on the consent page.")
        raise RuntimeError(f"{self.title} answered {r.status_code} {status}: {message}")

    def pages(self, url, key, limit=None, **params):
        """Every item under `key` across all pages, up to `limit`."""
        items, token = [], None
        while True:
            data = self.get(url, pageToken=token, **params)
            items.extend(data.get(key, []))
            token = data.get("nextPageToken")
            if not token or (limit and len(items) >= limit):
                return items[:limit] if limit else items
