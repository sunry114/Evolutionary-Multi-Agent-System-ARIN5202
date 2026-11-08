"""Credentials never live in project configuration or response objects."""
import os
import stat
from pathlib import Path


class CredentialError(ValueError):
    pass


def validate_key(value):
    value = value.strip()
    if not value or len(value) > 16384 or any(c.isspace() for c in value):
        raise CredentialError("Key must be a nonempty single token")
    return value


def key_path(provider):
    if provider not in ("hkgai", "deepseek"):
        raise CredentialError("Unknown credential provider")
    return Path.home() / ".config" / "evo-collab" / (provider + ".key")


def _check_directory(directory):
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CredentialError("Credential directory must be a real directory")
    if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise CredentialError("Credential directory requires owner and mode 0700")


def load_key(provider):
    path = key_path(provider)
    value = os.environ.get(provider.upper() + "_API_KEY", "").strip()
    if value:
        return validate_key(value)
    try:
        _check_directory(path.parent)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CredentialError("Credential must be a regular file")
            if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise CredentialError("Credential file requires owner and mode 0600")
            value = stream.read(16385).strip()
        return validate_key(value)
    except OSError:
        raise CredentialError("Credential unavailable; run setup-key or set provider API key") from None


def save_key(provider, value):
    if os.name != "posix":
        raise CredentialError("Persistent credential setup requires Linux; use an environment variable")
    value = validate_key(value)
    path = key_path(provider)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_directory(path.parent)
    # Exclusive creation refuses existing files and symlinks; never truncate a target.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value.strip() + "\n")
    return path
