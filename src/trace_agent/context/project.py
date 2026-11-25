"""Deterministic project detection: languages, package managers, frameworks, tests, git.

Everything here is computed locally from marker files and a bounded directory scan,
with no model calls, so it is fast, reproducible and cheap enough to run at the start
of every task. Detection rules are plain data tables so new ecosystems are a data change.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from trace_agent.context.ignore import DEFAULT_EXCLUDED_DIRS

MAX_SCAN_FILES = 5_000
MAX_MANIFEST_BYTES = 512_000
GIT_TIMEOUT_SECONDS = 5.0

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++",
    ".hpp": "C++", ".swift": "Swift", ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir",
    ".sh": "Shell", ".ps1": "PowerShell", ".vue": "Vue", ".svelte": "Svelte",
}

#: marker file -> (ecosystem, package manager or None)
MARKER_FILES: dict[str, tuple[str, str | None]] = {
    "pyproject.toml": ("Python", None),
    "setup.py": ("Python", "pip"),
    "setup.cfg": ("Python", None),
    "requirements.txt": ("Python", "pip"),
    "Pipfile": ("Python", "pipenv"),
    "poetry.lock": ("Python", "poetry"),
    "uv.lock": ("Python", "uv"),
    "package.json": ("JavaScript", None),
    "package-lock.json": ("JavaScript", "npm"),
    "yarn.lock": ("JavaScript", "yarn"),
    "pnpm-lock.yaml": ("JavaScript", "pnpm"),
    "bun.lockb": ("JavaScript", "bun"),
    "go.mod": ("Go", "go modules"),
    "Cargo.toml": ("Rust", "cargo"),
    "pom.xml": ("Java", "maven"),
    "build.gradle": ("Java", "gradle"),
    "build.gradle.kts": ("Kotlin", "gradle"),
    "Gemfile": ("Ruby", "bundler"),
    "composer.json": ("PHP", "composer"),
    "Dockerfile": ("Container", None),
    "docker-compose.yml": ("Container", None),
    "Makefile": ("Build", "make"),
}

#: dependency name (as it appears in manifests) -> display name
PYTHON_FRAMEWORKS = {
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI", "starlette": "Starlette",
    "sqlalchemy": "SQLAlchemy", "pydantic": "Pydantic", "celery": "Celery", "pandas": "pandas",
    "numpy": "NumPy", "torch": "PyTorch", "tensorflow": "TensorFlow", "streamlit": "Streamlit",
    "typer": "Typer", "click": "Click", "aiohttp": "aiohttp",
}
PYTHON_TEST_FRAMEWORKS = {"pytest": "pytest", "tox": "tox", "nox": "nox", "hypothesis": "Hypothesis"}
JS_FRAMEWORKS = {
    "react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "svelte": "Svelte",
    "@angular/core": "Angular", "express": "Express", "@nestjs/core": "NestJS", "fastify": "Fastify",
    "typescript": "TypeScript", "vite": "Vite", "prisma": "Prisma",
}
JS_TEST_FRAMEWORKS = {
    "jest": "Jest", "vitest": "Vitest", "mocha": "Mocha", "@playwright/test": "Playwright", "cypress": "Cypress",
}

TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "specs"}
TEST_FILE_PATTERNS = (
    re.compile(r"^test_.+\.py$"), re.compile(r"^.+_test\.py$"), re.compile(r"^.+\.(test|spec)\.[jt]sx?$"),
    re.compile(r"^.+_test\.go$"), re.compile(r"^.+Test\.(java|kt)$"), re.compile(r"^.+_spec\.rb$"),
)
NPM_DEFAULT_TEST = 'echo "Error: no test specified"'


class LanguageStat(BaseModel):
    name: str
    files: int


class GitInfo(BaseModel):
    branch: str | None = None
    modified: int = 0
    untracked: int = 0
    ahead_behind: str | None = None

    @property
    def dirty(self) -> bool:
        return self.modified > 0 or self.untracked > 0


class ProjectInfo(BaseModel):
    root: str
    languages: list[LanguageStat] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    test_command: str | None = None
    manifest_files: list[str] = Field(default_factory=list)
    test_dirs: list[str] = Field(default_factory=list)
    test_file_count: int = 0
    top_level: list[str] = Field(default_factory=list)
    file_count: int = 0
    scan_truncated: bool = False
    git: GitInfo | None = None

    @property
    def primary_language(self) -> str | None:
        return self.languages[0].name if self.languages else None


# --------------------------------------------------------------------------- detection


def detect_project(root: Path) -> ProjectInfo:
    root = root.resolve()
    info = ProjectInfo(root=root.as_posix())

    language_counts: Counter[str] = Counter()
    test_dirs: set[str] = set()
    test_files = 0
    scanned = 0
    for path in iter_project_files(root):
        scanned += 1
        if scanned > MAX_SCAN_FILES:
            info.scan_truncated = True
            break
        language = LANGUAGE_BY_EXTENSION.get(path.suffix.lower())
        if language:
            language_counts[language] += 1
        if any(p.match(path.name) for p in TEST_FILE_PATTERNS):
            test_files += 1
        for parent in path.relative_to(root).parents:
            if parent.name in TEST_DIR_NAMES:
                test_dirs.add(parent.as_posix())
    info.file_count = min(scanned, MAX_SCAN_FILES)
    info.languages = [LanguageStat(name=n, files=c) for n, c in language_counts.most_common()]
    info.test_dirs = sorted(test_dirs)[:10]
    info.test_file_count = test_files

    managers: list[str] = []
    for name, (_, manager) in MARKER_FILES.items():
        if (root / name).is_file():
            info.manifest_files.append(name)
            if manager and manager not in managers:
                managers.append(manager)

    py_deps = _python_dependencies(root)
    if (root / "pyproject.toml").is_file() and "poetry" in _read(root / "pyproject.toml") and "poetry" not in managers:
        managers.append("poetry")
    if py_deps and not any(m in managers for m in ("pip", "poetry", "uv", "pipenv")):
        managers.append("pip")
    js_deps, js_scripts = _js_manifest(root)
    if (root / "package.json").is_file() and not any(m in managers for m in ("npm", "yarn", "pnpm", "bun")):
        managers.append("npm")
    info.package_managers = managers

    info.frameworks = [d for k, d in PYTHON_FRAMEWORKS.items() if k in py_deps] + [
        d for k, d in JS_FRAMEWORKS.items() if k in js_deps
    ]
    info.test_frameworks = [d for k, d in PYTHON_TEST_FRAMEWORKS.items() if k in py_deps] + [
        d for k, d in JS_TEST_FRAMEWORKS.items() if k in js_deps
    ]
    if "pytest" not in info.test_frameworks and _uses_pytest(root, info):
        info.test_frameworks.insert(0, "pytest")
    info.test_command = _test_command(root, info, managers, js_scripts)
    info.top_level = _top_level(root)
    info.git = git_info(root)
    return info


def iter_project_files(root: Path):
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in DEFAULT_EXCLUDED_DIRS and not entry.name.endswith(".egg-info"):
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.M)


def _python_dependencies(root: Path) -> set[str]:
    names: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(_read(pyproject))
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project", {})
        specs = list(project.get("dependencies", []))
        for extra in (project.get("optional-dependencies") or {}).values():
            specs.extend(extra)
        for group in (data.get("dependency-groups") or {}).values():
            specs.extend(s for s in group if isinstance(s, str))
        poetry = data.get("tool", {}).get("poetry", {})
        specs.extend(poetry.get("dependencies", {}).keys())
        specs.extend(poetry.get("dev-dependencies", {}).keys())
        for group in (poetry.get("group") or {}).values():
            specs.extend((group.get("dependencies") or {}).keys())
        for spec in specs:
            match = _REQ_NAME.match(str(spec))
            if match:
                names.add(match.group(1).lower())
    for req in sorted(root.glob("requirements*.txt")) + sorted(root.glob("requirements/*.txt")):
        names.update(m.lower() for m in _REQ_NAME.findall(_read(req)) if not m.startswith("-"))
    for other in ("Pipfile", "setup.py", "setup.cfg"):
        text = _read(root / other).lower()
        for key in (*PYTHON_FRAMEWORKS, *PYTHON_TEST_FRAMEWORKS):
            if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", text):
                names.add(key)
    return {n.replace("_", "-") for n in names} | names


def _js_manifest(root: Path) -> tuple[set[str], dict[str, str]]:
    path = root / "package.json"
    if not path.is_file():
        return set(), {}
    try:
        data = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return set(), {}
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        deps.update((data.get(key) or {}).keys())
    scripts = {k: v for k, v in (data.get("scripts") or {}).items() if isinstance(v, str)}
    return deps, scripts


def _uses_pytest(root: Path, info: ProjectInfo) -> bool:
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return True
    if "[tool.pytest" in _read(root / "pyproject.toml"):
        return True
    return info.primary_language == "Python" and info.test_file_count > 0


def _test_command(root: Path, info: ProjectInfo, managers: list[str], scripts: dict[str, str]) -> str | None:
    if "pytest" in info.test_frameworks:
        return "python -m pytest"
    test_script = scripts.get("test")
    if test_script and NPM_DEFAULT_TEST not in test_script:
        runner = next((m for m in ("pnpm", "yarn", "bun", "npm") if m in managers), "npm")
        return f"{runner} test"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "pom.xml").is_file():
        return "mvn test"
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return "./gradlew test" if (root / "gradlew").is_file() else "gradle test"
    if info.primary_language == "Python" and info.test_file_count:
        return "python -m unittest"
    return None


def _top_level(root: Path, limit: int = 40) -> list[str]:
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    names = [
        e.name + ("/" if e.is_dir() else "")
        for e in entries
        if not (e.is_dir() and (e.name in DEFAULT_EXCLUDED_DIRS or e.name.endswith(".egg-info")))
    ]
    return names[:limit] + ([f"... {len(names) - limit} more"] if len(names) > limit else [])


def git_info(root: Path) -> GitInfo | None:
    """Branch and working-tree status, or None if git is unavailable or this is not a repository."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    info = GitInfo()
    for line in proc.stdout.splitlines():
        if line.startswith("## "):
            head = line[3:]
            branch, _, tracking = head.partition("...")
            info.branch = branch.replace("No commits yet on ", "").strip() or None
            bracket = re.search(r"\[(.+)\]", tracking)
            info.ahead_behind = bracket.group(1) if bracket else None
        elif line.startswith("??"):
            info.untracked += 1
        elif line.strip():
            info.modified += 1
    return info


# --------------------------------------------------------------------------- rendering


def render_project_info(info: ProjectInfo) -> str:
    lines = []
    if info.languages:
        langs = ", ".join(f"{s.name} ({s.files})" for s in info.languages[:6])
        lines.append(f"Languages (files): {langs}")
    if info.package_managers:
        lines.append(f"Package managers: {', '.join(info.package_managers)}")
    if info.frameworks:
        lines.append(f"Frameworks/libraries: {', '.join(info.frameworks)}")
    if info.test_frameworks or info.test_file_count:
        tf = ", ".join(info.test_frameworks) or "unknown framework"
        lines.append(f"Tests: {tf}; {info.test_file_count} test files; dirs: {', '.join(info.test_dirs) or '-'}")
    lines.append(f"Likely test command: {info.test_command or 'unknown'}")
    if info.manifest_files:
        lines.append(f"Manifest/config files: {', '.join(info.manifest_files)}")
    if info.git is None:
        lines.append("Git: not a git repository (or git unavailable)")
    else:
        g = info.git
        state = "clean" if not g.dirty else f"{g.modified} modified, {g.untracked} untracked"
        extra = f" [{g.ahead_behind}]" if g.ahead_behind else ""
        lines.append(f"Git: branch {g.branch or '?'}{extra}, working tree {state}")
    lines.append(f"Top level: {', '.join(info.top_level) or '(empty)'}")
    suffix = " (scan truncated)" if info.scan_truncated else ""
    lines.append(f"Files scanned: {info.file_count}{suffix}")
    return "\n".join(lines)
