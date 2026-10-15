"""
Encryption for stored platform tokens.

An access token for someone's YouTube channel is not ours to keep in plain
text, so a missing key is an error rather than a silent downgrade. Reading
stays tolerant: a value that will not decrypt comes back as None and the
account is marked for re-auth instead of crashing the caller.
"""

import settings


class SecretMissing(RuntimeError):
    pass


def _fernet():
    if not settings.SECRET_KEY:
        raise SecretMissing(
            "CLIPPER_SECRET_KEY is not set, so account tokens cannot be stored. "
            "Generate one with: python -c \"from cryptography.fernet import "
            "Fernet;print(Fernet.generate_key().decode())\"")
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise SecretMissing("pip install cryptography to store account tokens") from e
    return Fernet(settings.SECRET_KEY.encode())


def available():
    try:
        _fernet()
        return True
    except (SecretMissing, ValueError):
        return False


def seal(plain):
    """Encrypt a token for the database. Raises if no key is configured."""
    if plain is None or plain == "":
        return None
    return _fernet().encrypt(plain.encode()).decode()


def open_secret(sealed):
    """Decrypt, or None if the key is wrong, rotated or absent."""
    if not sealed:
        return None
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except Exception:
        return None
