#!/usr/bin/env python3
"""
Initialize a Git repository into the SWE pipeline shared workspace.

Clones the repo into:
    <repos_dir>/<repo_name>/

Then creates a .venv inside that repo directory and installs the repo's
dependencies into it:

  - If a requirements.txt exists at the repo root, pip-installs it.
  - If the registry entry in repos.json sets editable_install: true (or a
    setup.py / pyproject.toml exists and no requirements.txt), runs
    `pip install -e .` inside the venv.
  - For each name in the registry's install_extras list, runs
    `pip install -e .[<extra>]`.

CLI flags --editable / --extras override the registry entry, and --no-install
short-circuits all dependency installation.

The workspace layout is resolved (in order of precedence) from explicit
--repos-dir / --tasks-dir / --registry-file flags, then the SWE_REPOS_DIR /
SWE_TASKS_DIR / SWE_REGISTRY_FILE environment variables, then the SWE_DATA_DIR
environment variable (default /spellbook_data), from which
<data>/Serendipity/swe/workspace/repos, .../workspace/tasks, and
<data>/Serendipity/swe/repos.json are derived.

Usage:
    python3 initialize_repo.py <git_url>
    python3 initialize_repo.py <git_url> --force
    python3 initialize_repo.py <git_url> --editable --extras dev test
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DATA_DIR = '/spellbook_data'


def default_paths() -> tuple[Path, Path, Path]:
    """Resolve the default repos dir, tasks dir, and registry file.

    Precedence: the SWE_REPOS_DIR / SWE_TASKS_DIR / SWE_REGISTRY_FILE
    environment variables, else paths derived from SWE_DATA_DIR (default
    ``/spellbook_data``).

    :returns: Tuple of (repos_dir, tasks_dir, registry_file).
    """
    data_dir = Path(os.environ.get('SWE_DATA_DIR', DEFAULT_DATA_DIR))
    swe_dir = data_dir / 'Serendipity' / 'swe'
    workspace_dir = swe_dir / 'workspace'
    repos_dir = Path(os.environ.get('SWE_REPOS_DIR', str(workspace_dir / 'repos')))
    tasks_dir = Path(os.environ.get('SWE_TASKS_DIR', str(workspace_dir / 'tasks')))
    registry_file = Path(os.environ.get('SWE_REGISTRY_FILE', str(swe_dir / 'repos.json')))
    return repos_dir, tasks_dir, registry_file


def derive_repo_name(git_url: str) -> str:
    """Derive a directory name from a Git URL.

    Supports both HTTPS (https://github.com/owner/repo.git) and
    SSH (git@github.com:owner/repo.git) styles.
    """
    url = git_url.strip()
    if url.startswith('git@'):
        # git@host:owner/repo.git
        _, _, path = url.partition(':')
    else:
        path = urlparse(url).path

    name = path.rstrip('/').split('/')[-1]
    name = name.removesuffix('.git')
    if not name:
        raise ValueError(f"Could not derive repo name from URL: {git_url}")
    return name


def run(cmd, cwd=None, check=True):
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    result = subprocess.run(cmd, cwd=cwd, check=check)
    return result.returncode


def clone_repo(git_url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(['git', 'clone', git_url, str(dest)])


def resolve_python(python: str | None, registry_entry: dict) -> str:
    """Resolve the interpreter used to create the repo venv.

    Precedence: an explicit ``python`` argument, then the registry entry's
    ``python`` key, then the ``SWE_PYTHON`` environment variable, then the
    interpreter running this script (``sys.executable``).

    :param python: Explicit interpreter path, or None.
    :param registry_entry: The repo's repos.json entry (may be empty).
    :returns: The interpreter path to use.
    """
    if python:
        return python
    registry_python = registry_entry.get('python')
    if registry_python:
        return str(registry_python)
    return os.environ.get('SWE_PYTHON') or sys.executable


def create_venv(repo_dir: Path, python: str | None = None) -> Path:
    venv_dir = repo_dir / '.venv'
    run([python or sys.executable, '-m', 'venv', str(venv_dir)])
    return venv_dir


def upgrade_pip(venv_dir: Path) -> None:
    """Upgrade pip to the latest release in the venv.

    Always run (even under --no-install): the Phase A tooling uses this venv,
    and an outdated pip is itself flagged by the A9 dependency audit, so we
    keep it current to avoid false-positive 'vulnerable pip' findings.
    """
    pip = pip_path(venv_dir)
    print("Upgrading pip to the latest version ...")
    run([str(pip), 'install', '--upgrade', 'pip'])


def upgrade_setuptools(venv_dir: Path) -> None:
    """Upgrade setuptools to at least 83.0.0 in the venv.

    Always run (even under --no-install): the venv's bundled setuptools may
    ship with a version vulnerable to PYSEC-2026-3447, which the A9 dependency
    audit flags.  We pin the minimum to 83.0.0 to stay ahead of that CVE.
    """
    pip = pip_path(venv_dir)
    print("Upgrading setuptools to >=83.0.0 ...")
    run([str(pip), 'install', '--upgrade', 'setuptools>=83.0.0'])


def lookup_registry_entry(registry_file: Path, repo_name: str) -> dict:
    """Return the repos.json entry for repo_name, or {} if not present."""
    if not registry_file.is_file():
        return {}
    try:
        with open(registry_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: failed to read {registry_file}: {exc}", file=sys.stderr)
        return {}
    for entry in data.get('repos', []):
        if entry.get('name') == repo_name:
            return entry
        if entry.get('slug', '').rstrip('/').split('/')[-1] == repo_name:
            return entry
    return {}


def pip_path(venv_dir: Path) -> Path:
    pip = venv_dir / 'bin' / 'pip'
    if not pip.exists():
        # Windows fallback
        pip = venv_dir / 'Scripts' / 'pip.exe'
    return pip


def install_dependencies(
    repo_dir: Path,
    venv_dir: Path,
    editable: bool | None,
    extras: list[str],
    install_ruff: bool = True,
    install_interrogate: bool = True,
    install_mypy: bool = True,
    install_bandit: bool = True,
    install_vulture: bool = True,
    install_pip_audit: bool = True,
    install_pytest_cov: bool = True,
    install_radon: bool = True,
) -> dict:
    """Install the repo's dependencies into its venv.

    Returns a small dict summarizing what was done so the caller can log it.
    """
    pip = pip_path(venv_dir)
    summary = {'requirements_txt': False,
               'editable_install': False, 'extras_installed': [],
               'ruff_installed': False, 'interrogate_installed': False,
               'mypy_installed': False, 'bandit_installed': False,
               'vulture_installed': False, 'pip_audit_installed': False,
               'pytest_cov_installed': False, 'radon_installed': False}

    requirements = repo_dir / 'requirements.txt'
    has_setup = (repo_dir / 'setup.py').is_file() or (repo_dir / 'pyproject.toml').is_file()

    if requirements.is_file():
        print(f"Installing requirements from {requirements} ...")
        run([str(pip), 'install', '-r', str(requirements)])
        summary['requirements_txt'] = True

    # Decide whether to do an editable install. Explicit flag wins; otherwise
    # default to editable install when a setup.py/pyproject.toml exists and
    # there's no requirements.txt.
    if editable is None:
        editable = has_setup and not requirements.is_file()

    if editable:
        if not has_setup:
            print("WARNING: editable install requested but no setup.py / "
                  "pyproject.toml found; skipping.", file=sys.stderr)
        else:
            print("Installing package in editable mode (pip install -e .) ...")
            run([str(pip), 'install', '-e', '.'], cwd=str(repo_dir))
            summary['editable_install'] = True

    for extra in extras:
        if not has_setup:
            print(f"WARNING: extras '{extra}' requested but no setup.py / "
                  f"pyproject.toml found; skipping.", file=sys.stderr)
            break
        print(f"Installing extras: .[{extra}] ...")
        run([str(pip), 'install', '-e', f'.[{extra}]'], cwd=str(repo_dir))
        summary['extras_installed'].append(extra)

    if not (summary['requirements_txt'] or summary['editable_install']
            or summary['extras_installed']):
        print("No dependencies installed (no requirements.txt and no editable"
              " install configured).")

    if install_ruff:
        print("Installing ruff (linter) ...")
        run([str(pip), 'install', 'ruff'])
        summary['ruff_installed'] = True

    if install_interrogate:
        print("Installing interrogate (docstring coverage) ...")
        run([str(pip), 'install', 'interrogate'])
        summary['interrogate_installed'] = True

    if install_mypy:
        print("Installing mypy (type checker) ...")
        run([str(pip), 'install', 'mypy'])
        summary['mypy_installed'] = True

    if install_bandit:
        print("Installing bandit (security scanner) ...")
        run([str(pip), 'install', 'bandit'])
        summary['bandit_installed'] = True

    if install_vulture:
        print("Installing vulture (dead code detection) ...")
        run([str(pip), 'install', 'vulture'])
        summary['vulture_installed'] = True

    if install_pip_audit:
        print("Installing pip-audit (dependency vulnerability scanner) ...")
        run([str(pip), 'install', 'pip-audit'])
        summary['pip_audit_installed'] = True

    if install_pytest_cov:
        print("Installing pytest-cov (test coverage) ...")
        run([str(pip), 'install', 'pytest-cov'])
        summary['pytest_cov_installed'] = True

    if install_radon:
        print("Installing radon (complexity analysis) ...")
        run([str(pip), 'install', 'radon'])
        summary['radon_installed'] = True

    return summary


def cleanup_tasks(tasks_dir: Path, repo_name: str) -> None:
    """Remove all task directories for the given repo from the workspace.

    Task directories are named ``YYYYMMDD_<repo>_<task_id>`` under date-bucket
    subdirectories of ``tasks_dir``.
    """
    if not tasks_dir.is_dir():
        return
    count = 0
    for date_dir in sorted(tasks_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        for task_dir in sorted(date_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            if f'_{repo_name}' in task_dir.name:
                print(f"Removing task directory: {task_dir}")
                shutil.rmtree(task_dir)
                count += 1
    if count:
        print(f"Removed {count} task directories for {repo_name}.\n")
    else:
        print(f"No task directories found for {repo_name}.\n")


def initialize_repo(
    git_url: str,
    *,
    force: bool = False,
    editable: bool | None = None,
    extras: list[str] | None = None,
    no_install: bool = False,
    python: str | None = None,
    repos_dir: Path,
    tasks_dir: Path,
    registry_file: Path,
    install_ruff: bool = True,
    install_interrogate: bool = True,
    install_mypy: bool = True,
    install_bandit: bool = True,
    install_vulture: bool = True,
    install_pip_audit: bool = True,
    install_pytest_cov: bool = True,
    install_radon: bool = True,
) -> dict:
    """Clone and initialize a repo into the SWE shared workspace.

    Clones ``git_url`` into ``repos_dir/<repo_name>``, creates a ``.venv``,
    installs the repo's dependencies plus the Phase A tooling, and writes the
    ``.SWE/init_complete.marker`` that gates the venv-dependent pipeline stages.

    :param git_url: Git URL of the repo to clone (HTTPS or SSH).
    :param force: Remove any existing repo directory before cloning.
    :param editable: Force (True) or skip (False) the editable install; None
        falls back to the registry entry.
    :param extras: Extras to install; None falls back to the registry entry.
    :param no_install: Skip all dependency installation (still creates the venv).
    :param python: Interpreter used to create the repo venv; None falls back to
        the registry entry's ``python`` key, then the ``SWE_PYTHON`` env var,
        then ``sys.executable``.
    :param repos_dir: Directory the repo is cloned into.
    :param tasks_dir: Directory holding per-repo task subdirectories.
    :param registry_file: Path to the repos.json registry.
    :param install_ruff: Install ruff into the venv.
    :param install_interrogate: Install interrogate into the venv.
    :param install_mypy: Install mypy into the venv.
    :param install_bandit: Install bandit into the venv.
    :param install_vulture: Install vulture into the venv.
    :param install_pip_audit: Install pip-audit into the venv.
    :param install_pytest_cov: Install pytest-cov into the venv.
    :param install_radon: Install radon into the venv.
    :returns: Summary dict describing what was done.
    :raises FileExistsError: If the repo directory exists and ``force`` is False.
    :raises subprocess.CalledProcessError: If a git or pip step fails.
    """
    repo_name = derive_repo_name(git_url)
    repo_dir = repos_dir / repo_name
    registry_entry = lookup_registry_entry(registry_file, repo_name)
    interpreter = resolve_python(python, registry_entry)

    print(f"SWE workspace: {repos_dir}")
    print(f"Repo URL:      {git_url}")
    print(f"Repo name:     {repo_name}")
    print(f"Target dir:    {repo_dir}")
    print(f"Python:        {interpreter}")
    print(f"Force:         {force}")
    print()

    if repo_dir.exists():
        if force:
            print(f"Removing existing repo directory: {repo_dir}")
            shutil.rmtree(repo_dir)
            cleanup_tasks(tasks_dir, repo_name)
        else:
            raise FileExistsError(
                f"{repo_dir} already exists. "
                f"Re-run with --force to remove and re-clone."
            )

    clone_repo(git_url, repo_dir)
    venv_dir = create_venv(repo_dir, interpreter)
    upgrade_pip(venv_dir)
    upgrade_setuptools(venv_dir)

    resolved_editable = editable if editable is not None \
        else registry_entry.get('editable_install')
    resolved_extras = extras if extras is not None \
        else list(registry_entry.get('install_extras', []))

    if no_install:
        print("--no-install set; skipping dependency installation.")
        install_summary = {'skipped': True}
    else:
        install_summary = install_dependencies(
            repo_dir, venv_dir, editable=resolved_editable, extras=resolved_extras,
            install_ruff=install_ruff,
            install_interrogate=install_interrogate,
            install_mypy=install_mypy,
            install_bandit=install_bandit,
            install_vulture=install_vulture,
            install_pip_audit=install_pip_audit,
            install_pytest_cov=install_pytest_cov,
            install_radon=install_radon,
        )

    print()
    print("Done.")
    print(f"  Repo:           {repo_dir}")
    print(f"  Venv:           {venv_dir}")
    if registry_entry:
        print(f"  Registry entry: {registry_entry.get('name') or registry_entry.get('slug')}")
    print(f"  Install summary: {install_summary}")

    # Signal to the pipeline that initialization (clone + dependency install)
    # is complete.  Venv-dependent stages gate on this marker so they don't run
    # while dependencies are still being installed.  Written last, so it only
    # appears once every install step above has succeeded.
    init_marker = repo_dir / '.SWE' / 'init_complete.marker'
    init_marker.parent.mkdir(parents=True, exist_ok=True)
    init_marker.write_text('')
    print(f"  Init marker:    {init_marker}")

    return {
        'repo_name': repo_name,
        'repo_dir': str(repo_dir),
        'venv_dir': str(venv_dir),
        'registry_entry': registry_entry,
        'install_summary': install_summary,
        'init_marker': str(init_marker),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Initialize a Git repository into the SWE shared workspace."
    )
    parser.add_argument(
        'git_url',
        help="Git URL of the repo to clone (HTTPS or SSH).",
    )
    parser.add_argument(
        '--force',
        action='store_true',
        default=False,
        help="If set, remove any existing repo directory before cloning.",
    )
    parser.add_argument(
        '--editable',
        dest='editable',
        action='store_true',
        default=None,
        help="Force an editable install (pip install -e .). "
             "Overrides the registry entry.",
    )
    parser.add_argument(
        '--no-editable',
        dest='editable',
        action='store_false',
        help="Skip the editable install even if the registry requests one.",
    )
    parser.add_argument(
        '--extras',
        nargs='*',
        default=None,
        help="Extras to install via `pip install -e .[<extra>]`. "
             "Overrides the registry entry. Pass with no args to clear.",
    )
    parser.add_argument(
        '--no-install',
        action='store_true',
        default=False,
        help="Skip all dependency installation (still creates the venv).",
    )
    parser.add_argument(
        '--python',
        default=None,
        help="Interpreter used to create the repo venv (default: the registry "
             "entry's 'python' key, else the SWE_PYTHON env var, else the "
             "interpreter running this script).",
    )
    parser.add_argument(
        '--no-install-ruff',
        dest='install_ruff',
        action='store_false',
        default=True,
        help="Skip the default 'pip install ruff' step.",
    )
    parser.add_argument(
        '--no-install-interrogate',
        dest='install_interrogate',
        action='store_false',
        default=True,
        help="Skip the default 'pip install interrogate' step.",
    )
    parser.add_argument(
        '--no-install-mypy',
        dest='install_mypy',
        action='store_false',
        default=True,
        help="Skip the default 'pip install mypy' step.",
    )
    parser.add_argument(
        '--no-install-bandit',
        dest='install_bandit',
        action='store_false',
        default=True,
        help="Skip the default 'pip install bandit' step.",
    )
    parser.add_argument(
        '--no-install-vulture',
        dest='install_vulture',
        action='store_false',
        default=True,
        help="Skip the default 'pip install vulture' step.",
    )
    parser.add_argument(
        '--no-install-pip-audit',
        dest='install_pip_audit',
        action='store_false',
        default=True,
        help="Skip the default 'pip install pip-audit' step.",
    )
    parser.add_argument(
        '--no-install-pytest-cov',
        dest='install_pytest_cov',
        action='store_false',
        default=True,
        help="Skip the default 'pip install pytest-cov' step.",
    )
    parser.add_argument(
        '--no-install-radon',
        dest='install_radon',
        action='store_false',
        default=True,
        help="Skip the default 'pip install radon' step.",
    )
    parser.add_argument(
        '--repos-dir',
        default=None,
        help="Directory to clone the repo into (default: SWE_REPOS_DIR env var, "
             "else <SWE_DATA_DIR>/Serendipity/swe/workspace/repos).",
    )
    parser.add_argument(
        '--tasks-dir',
        default=None,
        help="Directory holding per-repo task subdirectories (default: "
             "SWE_TASKS_DIR env var, else <SWE_DATA_DIR>/Serendipity/swe/workspace/tasks).",
    )
    parser.add_argument(
        '--registry-file',
        default=None,
        help="Path to the repos.json registry (default: SWE_REGISTRY_FILE env "
             "var, else <SWE_DATA_DIR>/Serendipity/swe/repos.json).",
    )
    args = parser.parse_args()

    default_repos, default_tasks, default_registry = default_paths()
    repos_dir = Path(args.repos_dir) if args.repos_dir else default_repos
    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else default_tasks
    registry_file = Path(args.registry_file) if args.registry_file else default_registry

    try:
        initialize_repo(
            args.git_url,
            force=args.force,
            editable=args.editable,
            extras=args.extras,
            no_install=args.no_install,
            python=args.python,
            repos_dir=repos_dir,
            tasks_dir=tasks_dir,
            registry_file=registry_file,
            install_ruff=args.install_ruff,
            install_interrogate=args.install_interrogate,
            install_mypy=args.install_mypy,
            install_bandit=args.install_bandit,
            install_vulture=args.install_vulture,
            install_pip_audit=args.install_pip_audit,
            install_pytest_cov=args.install_pytest_cov,
            install_radon=args.install_radon,
        )
    except FileExistsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed: {exc}", file=sys.stderr)
        sys.exit(exc.returncode or 1)


if __name__ == '__main__':
    main()
