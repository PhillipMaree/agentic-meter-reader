from __future__ import annotations

import base64
import mimetypes
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from src.utils import PROJECT_ROOT, settings


class AuthError(RuntimeError):
    """Raised when credentials cannot be obtained or refreshed."""


class Client:
    """Context manager owning the Gmail service lifecycle.

    Usage:
        with Client() as gmail:
            msgs = gmail.list_unread(max_results=5)
    """

    def __init__(
        self,
        auth_dir: Path = PROJECT_ROOT / ".auth",
        scopes: tuple[str, ...] = (settings.mail.scope,),
        interactive: bool = True,
    ) -> None:
        self._credentials_path = auth_dir / "credentials.json"
        self._token_path = auth_dir / "token.json"
        self._scopes = list(scopes)
        self._interactive = interactive
        self._service: Resource | None = None

    # -- context protocol -------------------------------------------------

    def __enter__(self) -> "Client":
        creds = self._load_or_refresh()
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._service is not None:
            self._service.close()  # closes the underlying httplib2/requests session
            self._service = None
        # return None -> never swallow exceptions

    # -- auth internals ----------------------------------------------------

    def _load_or_refresh(self) -> Credentials:
        creds: Credentials | None = None

        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._token_path), self._scopes
            )

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._persist(creds)
            return creds

        if not self._interactive:
            raise AuthError(
                f"No valid token at {self._token_path} and interactive auth disabled. "
                "Run the bootstrap flow on a machine with a browser."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self._credentials_path), self._scopes
        )
        creds = flow.run_local_server(port=0, open_browser=True)
        self._persist(creds)
        return creds

    def _persist(self, creds: Credentials) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(creds.to_json())
        self._token_path.chmod(0o600)

    # -- API surface ---------------------------------------------------

    @property
    def service(self) -> Resource:
        if self._service is None:
            raise AuthError("Client used outside its context manager.")
        return self._service

    def list_messages(
        self, query: str = "in:inbox", max_results: int = 10
    ) -> list[dict]:
        """List message refs (id, threadId) matching a Gmail search query, newest first."""
        res = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return res.get("messages", [])

    def list_unread(self, max_results: int = 10) -> list[dict]:
        return self.list_messages(query="is:unread", max_results=max_results)

    def get_headers(
        self, msg_id: str, names: tuple[str, ...] = ("From", "Subject", "Date")
    ) -> dict[str, str]:
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata", metadataHeaders=list(names))
            .execute()
        )
        return {h["name"]: h["value"] for h in msg["payload"]["headers"]}

    def get_message(self, msg_id: str) -> dict:
        """Fetch a full message: id, thread id, headers, plain-text body, attachments.

        Each attachment is {"filename", "size" (bytes), "attachment_id"}; the
        id can be passed to the Gmail attachments API to download the data.
        """
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        return {
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "headers": {h["name"]: h["value"] for h in msg["payload"]["headers"]},
            "body": self._plain_text(msg["payload"]),
            "attachments": self._attachments(msg["payload"]),
        }

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: tuple[Path, ...] = (),
    ) -> dict:
        """Send a new mail with optional file attachments (e.g. to self for tests)."""
        mime = EmailMessage()
        mime["To"] = to
        mime["Subject"] = subject
        mime.set_content(body)
        for path in attachments:
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            mime.add_attachment(
                path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        return (
            self.service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )

    def trash_thread(self, thread_id: str) -> None:
        """Move a whole thread (mail + replies) to trash."""
        self.service.users().threads().trash(userId="me", id=thread_id).execute()

    def reply(self, original: dict, subject: str, body: str) -> dict:
        """Send a plain-text reply threaded onto the original message.

        `original` is the dict returned by get_message(). Replies go to the
        sender's Reply-To (falling back to From) and carry In-Reply-To /
        References headers so mail clients render them in the same thread.
        """
        headers = original["headers"]
        mime = EmailMessage()
        mime["To"] = headers.get("Reply-To") or headers["From"]
        mime["Subject"] = subject
        message_id = headers.get("Message-ID") or headers.get("Message-Id")
        if message_id:
            mime["In-Reply-To"] = message_id
            mime["References"] = (
                f"{headers['References']} {message_id}"
                if "References" in headers
                else message_id
            )
        mime.set_content(body)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        return (
            self.service.users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": original["thread_id"]})
            .execute()
        )

    def get_attachment(self, msg_id: str, attachment_id: str) -> bytes:
        """Download one attachment's raw bytes."""
        res = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=msg_id, id=attachment_id)
            .execute()
        )
        return base64.urlsafe_b64decode(res["data"])

    def mark_read(self, msg_id: str) -> None:
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()

    def mark_unread(self, msg_id: str) -> None:
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": ["UNREAD"]}
        ).execute()

    @staticmethod
    def _attachments(payload: dict) -> list[dict]:
        found = []
        if payload.get("filename"):
            body = payload.get("body", {})
            found.append(
                {
                    "filename": payload["filename"],
                    "size": body.get("size", 0),
                    "attachment_id": body.get("attachmentId"),
                }
            )
        for part in payload.get("parts", []):
            found.extend(Client._attachments(part))
        return found

    @staticmethod
    def _plain_text(payload: dict) -> str:
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(data).decode() if data else ""
        for part in payload.get("parts", []):
            text = Client._plain_text(part)
            if text:
                return text
        return ""
