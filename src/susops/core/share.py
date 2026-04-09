"""Encrypted file sharing server and client for SusOps.

Replaces the bash implementation that used nc + openssl + gzip.
Uses aiohttp for async HTTP serving + cryptography library for AES-256-CTR encryption.

Protocol (same as original):
- HTTP Basic auth with credentials ":password" (empty username)
- File is gzip-compressed then AES-256-CTR encrypted with PBKDF2 key derivation
- Original filename is AES-encrypted and base64-encoded in Content-Disposition
- Served as application/octet-stream

Requires: cryptography>=42, aiohttp>=3.9 (install with pip install susops[share])
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import os
import secrets
import string
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from susops.core.types import ShareInfo

if TYPE_CHECKING:
    pass

__all__ = ["ShareServer", "fetch_file", "generate_password", "ShareInfo"]


def _require_crypto() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "File sharing requires the 'cryptography' package. "
            "Install with: pip install 'susops[share]'"
        ) from e


def _require_aiohttp() -> None:
    try:
        import aiohttp  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "File sharing requires the 'aiohttp' package. "
            "Install with: pip install 'susops[share]'"
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


# ---------------------------------------------------------------------------
# Shared event loop — all ShareServer instances and the StatusServer reuse one
# daemon thread + asyncio loop to avoid creating multiple threads.
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return (and lazily initialise) the shared daemon event loop."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            t = threading.Thread(
                target=_loop.run_forever,
                name="susops-async-loop",
                daemon=True,
            )
            t.start()
        return _loop


class ShareServer:
    """Async HTTP file share server with AES-256-CTR encryption.

    The file is encrypted in memory and served via HTTP Basic auth using aiohttp.
    Multiple ShareServer instances share one background event loop thread.
    """

    def __init__(self) -> None:
        self._runner = None
        self._port: int = 0
        self._encrypted_data: bytes = b""
        self._encrypted_filename: str = ""
        self._password: str = ""

    def start(
        self,
        file_path: Path,
        password: str,
        port: int = 0,
        workspace: Path | None = None,
    ) -> ShareInfo:
        """Encrypt and start serving the file asynchronously.

        Returns ShareInfo with the URL and credentials.
        """
        _require_crypto()
        _require_aiohttp()

        if self._runner is not None:
            raise RuntimeError("Share server is already running")

        self._encrypted_data = _compress_and_encrypt(file_path, password)
        self._encrypted_filename = _encrypt_filename(file_path.name, password)
        self._password = password

        loop = _get_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._start_async(port), loop
        )
        self._port = future.result(timeout=10)

        return ShareInfo(
            file_path=str(file_path),
            port=self._port,
            password=password,
            url=f"http://localhost:{self._port}",
        )

    async def _start_async(self, port: int) -> int:
        from aiohttp import web

        async def handle(request: web.Request) -> web.Response:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return web.Response(
                    status=401,
                    headers={"WWW-Authenticate": 'Basic realm="susops share"'},
                )
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, _, pw = decoded.partition(":")
            except Exception:
                return web.Response(status=401)
            if pw != self._password:
                return web.Response(
                    status=401,
                    headers={"WWW-Authenticate": 'Basic realm="susops share"'},
                )

            return web.Response(
                body=self._encrypted_data,
                content_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{self._encrypted_filename}"',
                    "Connection": "close",
                },
            )

        app = web.Application()
        app.router.add_get("/", handle)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        self._runner = runner
        # Resolve the actual bound port (important when port=0)
        return site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    def stop(self) -> None:
        """Stop the share server."""
        if self._runner is not None:
            loop = _get_loop()
            future = asyncio.run_coroutine_threadsafe(
                self._runner.cleanup(), loop
            )
            try:
                future.result(timeout=5)
            except Exception:
                pass
            self._runner = None
        self._port = 0

    def is_running(self) -> bool:
        return self._runner is not None

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

    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(
        _fetch_async(host, port, password, outfile), loop
    )
    return future.result(timeout=60)


async def _fetch_async(
    host: str,
    port: int,
    password: str,
    outfile: Path | None,
) -> Path:
    _require_aiohttp()
    import aiohttp

    url = f"http://{host}:{port}"
    credentials = base64.b64encode(f":{password}".encode()).decode()

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers={"Authorization": f"Basic {credentials}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Share server returned HTTP {resp.status}")

            content_disp = resp.headers.get("Content-Disposition", "")
            encrypted_filename = ""
            for part in content_disp.split(";"):
                part = part.strip()
                if part.startswith("filename="):
                    encrypted_filename = part[9:].strip('"')
                    break

            encrypted_data = await resp.read()

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
