"""Secret resolution — read sensitive values from a file or the environment.

Every sensitive value (COM shared secret, target passwords / client secrets /
tokens, the queue connection string) is resolved through `get_secret()` instead
of `os.environ` directly, so operators can keep secrets **out of the process
environment** and `.env` files.

Resolution order for a name `FOO`:

  1. ``FOO_FILE`` — if set, read the secret from that file path (trailing
     newline stripped). This is the vendor-neutral "secret as a file" convention
     that every major vault already projects onto disk:
       * Azure Key Vault  → Secrets Store CSI driver (mounts under a volume),
       * AWS Secrets Manager → the ASCP CSI provider (same),
       * HashiCorp Vault  → Vault Agent / CSI renders to a tmpfs file,
       * Docker / Compose / Swarm → ``/run/secrets/<name>`` (tmpfs),
       * systemd → ``LoadCredential=`` exposes ``$CREDENTIALS_DIRECTORY/<name>``.
  2. ``FOO`` — the plain environment variable (unchanged legacy behaviour,
     convenient for local development via ``.env``).
  3. otherwise the `default` is returned, or — if the secret is `required` and no
     default was given — a `KeyError` is raised so the app fails fast at startup.

Reading from a file is preferred in production: file contents don't leak into
``docker inspect``, ``/proc/<pid>/environ``, crash dumps, or child processes the
way environment variables do, and a CSI/Agent-mounted file can be rotated
without changing the deployment's env.
"""

from __future__ import annotations

import os

__all__ = ["get_secret"]

_MISSING = object()


def get_secret(
    name: str,
    default: str | None = None,
    *,
    required: bool = True,
) -> str | None:
    """Resolve secret `name` from ``<name>_FILE``, then env ``<name>``.

    Args:
        name: The base variable name, e.g. ``"OBM_PASSWORD"``. The file-backed
            form is ``"OBM_PASSWORD_FILE"``.
        default: Value to return when neither source is set. If omitted and
            `required` is True, a missing secret raises `KeyError`.
        required: When True (the default) and no value or `default` is found,
            raise `KeyError` so startup fails fast rather than running with a
            silently-missing credential.

    Returns:
        The secret string (file contents win over the plain env var), or
        `default` / `None` when unset and not required.
    """
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                # Strip only a trailing newline so files written with `echo`
                # (which appends "\n") work, without corrupting a secret that
                # legitimately contains leading/trailing spaces mid-value.
                return fh.read().rstrip("\n")
        except OSError as e:
            raise KeyError(
                f"{name}_FILE is set to '{file_path}' but it could not be read: {e}"
            ) from e

    value = os.environ.get(name, _MISSING)  # type: ignore[arg-type]
    if value is not _MISSING:
        return value  # type: ignore[return-value]

    if default is not None or not required:
        return default

    raise KeyError(
        f"Missing required secret '{name}'. Set the environment variable "
        f"'{name}', or point '{name}_FILE' at a file containing it (e.g. a "
        f"Docker/CSI/systemd-projected secret)."
    )
