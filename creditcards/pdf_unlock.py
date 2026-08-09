"""Decrypt password-protected statement PDFs, entirely in memory.

Decrypted bytes are never written to disk and never leave the process except as
parsed transactions. The password itself is read from the environment inside
CardConfig.password() and is not logged or returned in any API response.
"""

import io

import pikepdf


class PdfError(RuntimeError):
    pass


class WrongPassword(PdfError):
    pass


def is_encrypted(data: bytes) -> bool:
    try:
        with pikepdf.open(io.BytesIO(data)):
            return False
    except pikepdf.PasswordError:
        return True
    except pikepdf.PdfError as exc:
        raise PdfError(f"Not a readable PDF: {exc}")


def decrypt(data: bytes, password: str) -> bytes:
    """Return an unencrypted copy of `data`.

    Handles the un-encrypted case too, so callers do not have to branch.
    """
    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            return _save(pdf)
    except pikepdf.PasswordError:
        pass
    except pikepdf.PdfError as exc:
        raise PdfError(f"Not a readable PDF: {exc}")

    if not password:
        raise WrongPassword("PDF is encrypted but no password was configured")
    try:
        with pikepdf.open(io.BytesIO(data), password=password) as pdf:
            return _save(pdf)
    except pikepdf.PasswordError:
        raise WrongPassword(
            "PDF password was rejected. Check the card's password env var — "
            "banks often use a pattern like name+DOB or card digits+DOB."
        )
    except pikepdf.PdfError as exc:
        raise PdfError(f"Could not open PDF after decryption: {exc}")


def _save(pdf) -> bytes:
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()
