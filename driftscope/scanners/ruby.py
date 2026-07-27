"""Ruby scanner: Gemfile, Gemfile.lock, *.gemspec, .ruby-version.

Four surfaces, and the ordering between two of them matters:

  Gemfile        gem "rails", "~> 7.0"     -> a constraint, what is permitted
  Gemfile.lock   rails (7.0.4)             -> the resolved pin, what actually ships
  *.gemspec      add_dependency "x", "~>"  -> a library's declared range
  .ruby-version  3.1.2                     -> the runtime

The lock file is authoritative -- it is what `bundle install --deployment` and
every CI run actually resolve to -- so a lock entry wins the dedupe over the
Gemfile constraint for the same gem, exactly as `.terraform.lock.hcl` beats a
`required_providers` range. Both are still emitted: a constraint floor that
permits something ancient is its own finding even when today's lock is current.

Gemfile and gemspec are Ruby programs, not data. Anything interpolated, computed
or read from a variable is recorded as unresolved rather than guessed at -- a
version string that was never literally written must not be invented.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import RUBY_RUNTIME, RUBYGEMS, Component
from . import read_text, rel

# gem "name", "~> 1.2", ">= 1.2.3", require: false
_GEM_RE = re.compile(
    r"""^\s*gem\s+["'](?P<name>[\w.\-]+)["']\s*(?P<rest>.*)$""", re.M
)
# add_dependency / add_runtime_dependency / add_development_dependency
_GEMSPEC_DEP_RE = re.compile(
    r"""^\s*\w+\.add(?:_runtime|_development)?_dependency\s*\(?\s*"""
    r"""["'](?P<name>[\w.\-]+)["']\s*(?P<rest>.*)$""",
    re.M,
)
# The first quoted version-looking token in the remainder of a gem line.
_VERSION_TOKEN = re.compile(r"""["'](?P<spec>[~><=!\s]*\d[\w.\-]*)["']""")
# ruby "3.1.2" in a Gemfile, or required_ruby_version in a gemspec
_RUBY_DIRECTIVE = re.compile(r"""^\s*ruby\s+["'](?P<v>[\d][\w.\-]*)["']""", re.M)
_REQUIRED_RUBY = re.compile(
    r"""required_ruby_version\s*=\s*["'](?P<v>[^"']+)["']"""
)
# Non-literal versions: a constant, a variable, interpolation, a File.read.
_NON_LITERAL = re.compile(r"""["'][^"']*#\{|:?[A-Z_]{2,}\b|File\.|ENV\[""")

# Gemfile.lock `specs:` block: two-space indent is a gem, four is its dependency.
_LOCK_SPEC_RE = re.compile(r"^ {4}(?P<name>[\w.\-]+) \((?P<version>[^)]+)\)$", re.M)
_LOCK_RUBY_RE = re.compile(r"^RUBY VERSION\s*\n\s*ruby (?P<v>[\d][\w.\-]*)", re.M)
_LOCK_BUNDLED_RE = re.compile(r"^BUNDLED WITH\s*\n\s*(?P<v>[\d][\w.\-]*)", re.M)

_GROUP_RE = re.compile(r"^\s*group\s+(?P<groups>[^\n]*?)\s*do\s*$", re.M)


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def _group_at(text: str, idx: int) -> str | None:
    """The innermost `group :x do` enclosing `idx`, by nesting depth.

    Approximate on purpose: tracking `do`/`end` pairs exactly would mean parsing
    Ruby. Getting the group slightly wrong mislabels a dependency; it never
    invents one, so the failure mode is cosmetic.
    """
    best = None
    for m in _GROUP_RE.finditer(text):
        if m.start() > idx:
            break
        best = m
    if best is None:
        return None
    names = re.findall(r":(\w+)", best.group("groups"))
    return ",".join(names) or None


def _spec_from(rest: str) -> tuple[str | None, str | None]:
    """(constraint, unresolved-reason) from the tail of a gem line."""
    # Strip trailing options (require:, path:, git:) before looking for a version.
    head = re.split(r",\s*\w+:", rest, maxsplit=1)[0]
    m = _VERSION_TOKEN.search(head)
    if m:
        return m.group("spec").strip(), None
    if _NON_LITERAL.search(head):
        return None, head.strip()[:80]
    return None, None


def _floor(spec: str | None) -> str | None:
    from ..version import constraint_floor

    return constraint_floor(spec) if spec else None


def scan_file(path: Path, root: Path) -> list[Component]:
    name = path.name.lower()
    if name == "gemfile.lock":
        return _scan_lock(path, root)
    if name == "gemfile":
        return _scan_gemfile(path, root)
    if path.suffix == ".gemspec":
        return _scan_gemspec(path, root)
    if name == ".ruby-version":
        return _scan_ruby_version(path, root)
    return []


def _scan_gemfile(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []

    for m in _RUBY_DIRECTIVE.finditer(text):
        out.append(Component(
            ecosystem=RUBY_RUNTIME, name="ruby", current=m.group("v"),
            source=src, line=_line_of(text, m.start()), raw=m.group("v"),
            meta={"runtime": True, "declared_in": "Gemfile"},
        ))

    for m in _GEM_RE.finditer(text):
        spec, unresolved = _spec_from(m.group("rest"))
        meta: dict = {"group": _group_at(text, m.start())}
        if unresolved:
            # A computed version is not a version. Record the expression so the
            # report can say why it could not be assessed.
            meta["unresolved_expr"] = unresolved
        out.append(Component(
            ecosystem=RUBYGEMS, name=m.group("name"),
            current=_floor(spec), source=src,
            line=_line_of(text, m.start()), raw=spec,
            meta=meta,
        ))
    return out


def _scan_lock(path: Path, root: Path) -> list[Component]:
    """Gemfile.lock: the resolved graph, and the ruby/bundler versions it pins."""
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []

    m = _LOCK_RUBY_RE.search(text)
    if m:
        out.append(Component(
            ecosystem=RUBY_RUNTIME, name="ruby",
            # `ruby 3.1.2p20` -- the patchlevel suffix is not part of the version.
            current=re.sub(r"p\d+$", "", m.group("v")),
            source=src, line=_line_of(text, m.start()), raw=m.group("v"),
            meta={"runtime": True, "locked": True, "declared_in": "Gemfile.lock"},
        ))

    seen: set[str] = set()
    for m in _LOCK_SPEC_RE.finditer(text):
        gem = m.group("name")
        if gem in seen:
            continue  # a gem appears once as a spec, then again under dependents
        seen.add(gem)
        out.append(Component(
            ecosystem=RUBYGEMS, name=gem, current=m.group("version"),
            source=src, line=_line_of(text, m.start()), raw=m.group("version"),
            meta={"locked": True},
        ))

    m = _LOCK_BUNDLED_RE.search(text)
    if m:
        out.append(Component(
            ecosystem=RUBYGEMS, name="bundler", current=m.group("v"),
            source=src, line=_line_of(text, m.start()), raw=m.group("v"),
            meta={"locked": True, "tool": True},
        ))
    return out


def _scan_gemspec(path: Path, root: Path) -> list[Component]:
    text = read_text(path)
    if not text:
        return []
    src = rel(path, root)
    out: list[Component] = []

    m = _REQUIRED_RUBY.search(text)
    if m:
        floor = _floor(m.group("v"))
        if floor:
            out.append(Component(
                ecosystem=RUBY_RUNTIME, name="ruby", current=floor,
                source=src, line=_line_of(text, m.start()), raw=m.group("v"),
                meta={"runtime": True, "constraint": True,
                      "declared_in": path.name},
            ))

    for m in _GEMSPEC_DEP_RE.finditer(text):
        spec, unresolved = _spec_from(m.group("rest"))
        meta: dict = {
            "group": "development" if "_development_" in m.group(0) else None,
            "gemspec": True,
        }
        if unresolved:
            meta["unresolved_expr"] = unresolved
        out.append(Component(
            ecosystem=RUBYGEMS, name=m.group("name"),
            current=_floor(spec), source=src,
            line=_line_of(text, m.start()), raw=spec, meta=meta,
        ))
    return out


def _scan_ruby_version(path: Path, root: Path) -> list[Component]:
    text = (read_text(path) or "").strip()
    # Tolerates "3.1.2", "ruby-3.1.2" (rbenv/rvm) and a trailing patchlevel.
    m = re.match(r"^(?:ruby-)?(?P<v>\d[\w.\-]*)$", text)
    if not m:
        return []
    return [Component(
        ecosystem=RUBY_RUNTIME, name="ruby",
        current=re.sub(r"p\d+$", "", m.group("v")),
        source=rel(path, root), line=1, raw=text,
        meta={"runtime": True, "declared_in": ".ruby-version"},
    )]
