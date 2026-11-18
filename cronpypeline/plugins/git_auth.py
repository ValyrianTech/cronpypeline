"""Shared git authentication helpers for the SWE pipeline plugins.

Git operations that talk to a remote (``git push``, ``git clone``) must
authenticate with the pipeline's configured GitHub token rather than with
whatever credential the ambient git configuration happens to hold.  A stale
entry in ``~/.git-credentials`` would otherwise fail the operation with a 403
naming an unrelated account, while every GitHub API call — which uses the
configured token — kept succeeding.
"""

import os

AUTH_TOKEN_ENV = "SWE_GIT_AUTH_TOKEN"

_CREDENTIAL_HELPER = (
    "credential.helper=!f() { echo username=x-access-token; "
    'echo "password=$' + AUTH_TOKEN_ENV + '"; }; f'
)


def auth_config_args() -> list[str]:
    """Return the ``git -c`` arguments that authenticate with the token.

    The ambient credential helpers are cleared first, so a stale entry in
    ``~/.git-credentials`` cannot override the configured token.

    :returns: Arguments to splice into a git command line.
    """
    return ["-c", "credential.helper=", "-c", _CREDENTIAL_HELPER]


def auth_env(token: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment that exposes ``token`` to the credential helper.

    The token travels through the environment rather than the command line, so
    it never appears in the process argument list (visible via ``ps``) nor in
    git's error output.

    :param token: GitHub token used to authenticate the git operation.
    :param base: Base environment; defaults to a copy of ``os.environ``.
    :returns: Environment dict for the subprocess.
    """
    env = dict(os.environ if base is None else base)
    env[AUTH_TOKEN_ENV] = token
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
