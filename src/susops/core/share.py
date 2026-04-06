"""Encrypted file sharing server and client for SusOps.

Replaces the bash implementation that used nc + openssl + gzip.
Uses Python's http.server + cryptography library for AES-256-CTR encryption.

Protocol (same as original):
- HTTP Basic auth with credentials ":password" (empty username)
- File is gzip-compressed then AES-256-CTR encrypted with PBKDF2 key derivation
- Original filename is AES-encrypted and base64-encoded in Content-Disposition
- Served as application/octet-stream

Requires: cryptography>=42 (install with pip install susops[crypto])
"""
from __future__ import annotations

import base64
import gzip
import os
import secrets
import string
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from susops.core.types import ShareInfo

__all__ = ["ShareServer", "fetch_file", "generate_password", "ShareInfo"]


def _require_crypto() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "File sharing requires the 'cryptography' package. "
            "Install with: pip install 'susops[crypto]'"
        ) from e


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte AES key from a password using PBKDF2-HMAC-SHA256."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    return kdf.derive(password.encode())


def _encrypt(data: bytes, password: str) -> bytes:
    """Encrypt bytes using AES-256-CTR.

    Format: magic(8) + salt(8) + iv(16) + ciphertext
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = os.urandom(8)
    key = _derive_key(password, salt)
    iv = os.urandom(16)

    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(data) + encryptor.finalize()

    return b"Salted__" + salt + iv + ciphertext


def _decrypt(data: bytes, password: str) -> bytes:
    """Decrypt bytes produced by _encrypt()."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not data.startswith(b"Salted__"):
        raise ValueError("Invalid encrypted data: missing 'Salted__' header")
    salt = data[8:16]
    iv = data[16:32]
    ciphertext = data[32:]

    key = _derive_key(password, salt)
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def _compress_and_encrypt(file_path: Path, password: str) -> bytes:
    """Gzip-compress then AES-256-CTR encrypt a file."""
    raw = file_path.read_bytes()
    compressed = gzip.compress(raw)
    return _encrypt(compressed, password)


def _decrypt_and_decompress(data: bytes, password: str) -> bytes:
    """AES-256-CTR decrypt then gunzip."""
    compressed = _decrypt(data, password)
    return gzip.decompress(compressed)


def _encrypt_filename(filename: str, password: str) -> str:
    """Encrypt a filename string and return base64-encoded ciphertext."""
    encrypted = _encrypt(filename.encode(), password)
    return base64.urlsafe_b64encode(encrypted).decode()


def _decrypt_filename(encrypted_b64: str, password: str) -> str:
    """Decrypt a base64-encoded encrypted filename."""
    encrypted = base64.urlsafe_b64decode(encrypted_b64.encode())
    return _decrypt(encrypted, password).decode()


def generate_password(length: int = 24) -> str:
    """Generate a secure random password (alphanumeric)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class _ShareHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves a single encrypted file with Basic auth."""

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            _, _, pw = decoded.partition(":")
            return pw == self.server.share_password  # type: ignore[attr-defined]
        except Exception:
            return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="susops share"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        encrypted_data: bytes = self.server.share_data  # type: ignore[attr-defined]
        encrypted_filename: str = self.server.share_filename_enc  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(encrypted_data)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{encrypted_filename}"',
        )
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encrypted_data)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802
        pass


class ShareServer:
    """HTTP file share server with AES-256-CTR encryption.

    The file is encrypted in memory and served via HTTP Basic auth.
    Replaces the bash nc + openssl + FIFO approach.
    """

    def __init__(self) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0

    def start(
        self,
        file_path: Path,
        password: str,
        port: int = 0,
        workspace: Path | None = None,
    ) -> ShareInfo:
        """Encrypt and start serving the file.

        Returns ShareInfo with the URL and credentials.
        """
        _require_crypto()

        if self._server is not None:
            raise RuntimeError("Share server is already running")

        encrypted_data = _compress_and_encrypt(file_path, password)
        encrypted_filename = _encrypt_filename(file_path.name, password)

        server = HTTPServer(("127.0.0.1", port), _ShareHandler)
        server.share_data = encrypted_data  # type: ignore[attr-defined]
        server.share_password = password  # type: ignore[attr-defined]
        server.share_filename_enc = encrypted_filename  # type: ignore[attr-defined]

        self._server = server
        self._port = server.server_address[1]

        self._thread = threading.Thread(
            target=server.serve_forever,
            name="susops-share-server",
            daemon=True,
        )
        self._thread.start()

        return ShareInfo(
            file_path=str(file_path),
            port=self._port,
            password=password,
            url=f"http://localhost:{self._port}",
        )

    def stop(self) -> None:
        """Stop the share server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._port = 0

    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def get_port(self) -> int:
        return self._port


def fetch_file(
    host: str,
    port: int,
    password: str,
    outfile: Path | None = None,
) -> Path:
    """Download and decrypt a file from a ShareServer.

    Args:
        host: Hostname or IP (e.g. "localhost").
        port: Port the share server is listening on.
        password: Decryption password.
        outfile: Where to save the file. Defaults to ~/Downloads/<original_name>.

    Returns:
        Path to the saved decrypted file.
    """
    _require_crypto()

    url = f"http://{host}:{port}"
    credentials = base64.b64encode(f":{password}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {credentials}"})

    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Share server returned HTTP {resp.status}")

        content_disp = resp.headers.get("Content-Disposition", "")
        encrypted_filename = ""
        for part in content_disp.split(";"):
            part = part.strip()
            if part.startswith("filename="):
                encrypted_filename = part[9:].strip('"')
                break

        encrypted_data = resp.read()

    decrypted = _decrypt_and_decompress(encrypted_data, password)

    if outfile is None:
        if encrypted_filename:
            try:
                original_name = _decrypt_filename(encrypted_filename, password)
            except Exception:
                original_name = f"download_{secrets.token_hex(4)}"
        else:
            original_name = f"download_{secrets.token_hex(4)}"

        downloads = Path.home() / "Downloads"
        downloads.mkdir(exist_ok=True)
        outfile = downloads / original_name

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_bytes(decrypted)
    return outfile
