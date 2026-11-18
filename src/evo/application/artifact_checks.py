"""Validate objective artifact expectations and verify scoped workspace output."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html.parser import HTMLParser

from evo.adapters.tools.filesystem import validate_file_path
from evo.ports.harness import ArtifactRef


SLIDE_FORMAT = ("Use purpose slides for .html files. Write standalone HTML with at least two "
                "nonempty section elements with unique ids. Link to each next section with "
                "a native fragment link inside the preceding section.")


class SlideDeckParser(HTMLParser):
    """Recognize the native, offline slide navigation supported by the verifier."""

    def __init__(self):
        super().__init__()
        self.sections = []
        self.current = None
        self.ids = set()
        self.valid = True
        self.ignored = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get("id")
        if ident:
            if ident in self.ids:
                self.valid = False
            self.ids.add(ident)
        if tag == "section":
            if self.current is not None or not ident or any(c.isspace() for c in ident):
                self.valid = False
            self.current = {"id": ident, "links": set(), "text": False}
            self.sections.append(self.current)
        elif tag == "a" and self.current is not None:
            self.current["links"].add(attrs.get("href"))
        elif tag in ("script", "style"):
            self.ignored = True

    def handle_endtag(self, tag):
        if tag == "section":
            self.current = None
        elif tag in ("script", "style"):
            self.ignored = False

    def handle_data(self, data):
        if self.current is not None and not self.ignored and data.strip():
            self.current["text"] = True

    def navigable(self):
        return (self.valid and self.current is None and len(self.sections) >= 2
                and all(section["text"] for section in self.sections)
                and all("#" + nxt["id"] in prev["links"]
                        for prev, nxt in zip(self.sections, self.sections[1:])))


@dataclass(frozen=True)
class ArtifactCheck:
    path: str
    purpose: str | None = None
    contains: str = ""
    exact: str | None = None

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path or not isinstance(self.contains, str) or (
                self.exact is not None and not isinstance(self.exact, str)) or (
                not self.contains and self.exact is None):
            raise ValueError("Artifact check needs a path and expected content")

        if self.purpose is None:
            object.__setattr__(self, "purpose", "slides" if self.path.lower().endswith(".html") else "document")

    def verify(self, workspace, artifacts: tuple[ArtifactRef, ...]) -> bool:
        # A pre-existing file and a model-provided evidence string are insufficient.
        artifact = next((item for item in artifacts
                         if item.path == self.path and item.purpose == self.purpose), None)
        if artifact is None:
            return False
        try:
            content = workspace.read_file(self.path, purpose=self.purpose)
        except (OSError, ValueError):
            return False
        return (hashlib.sha256(content.encode("utf-8")).hexdigest() == artifact.sha256
                and (self.exact is None or content == self.exact)
                and self.contains in content
                and (self.purpose != "slides" or self._verify_slides(content)))

    @staticmethod
    def _verify_slides(content):
        parser = SlideDeckParser()
        try:
            parser.feed(content)
            parser.close()
        except (ValueError, AssertionError):
            return False
        return parser.navigable()


def parse_checks(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 5:
        raise ValueError("Provide 1 to 5 objective subtask checks")
    checks = {}
    for key, raw in value.items():
        if key not in ("s1", "s2", "s3", "s4", "s5") or not isinstance(raw, dict):
            raise ValueError("Invalid subtask check")
        if "path" not in raw or set(raw) - {"path", "purpose", "contains", "exact"}:
            raise ValueError("Invalid subtask check fields")
        check = ArtifactCheck(**raw)
        validate_file_path(check.path, check.purpose)
        if len(check.contains.encode()) > 8192 or (
                check.exact is not None and len(check.exact.encode()) > 8192):
            raise ValueError("Objective check is oversized")
        checks[key] = check
    return checks
