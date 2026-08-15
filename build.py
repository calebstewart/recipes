#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML>=6.0"]
# ///
"""Static site generator for the recipe collection.

Reads the Markdown recipes in the category directories, parses the YAML
frontmatter and the body, and writes a static site to the output directory: a
homepage with cards and a client-side search index, one page per recipe with
schema.org Recipe markup (both JSON-LD and microdata), a web app manifest and
service worker so the site installs on a phone, and the static assets.

Dependencies are declared in the PEP 723 header above, so `uv run build.py`
resolves them with nothing installed. The Nix dev shell provides the same
packages, so a plain `python3 build.py` works there too.

Usage:
    uv run build.py [--out dist] [--base-url /recipes/] [--serve]
    python3 build.py ...        # inside the dev shell
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from urllib.parse import urljoin, urlparse

import yaml

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent

SITE_TITLE = "Recipes"
AUTHOR_NAME = "Caleb Stewart"
SITE_DESCRIPTION = "A personal collection of recipes, kept as plain Markdown."

DEFAULT_REPOSITORY = "calebstewart/recipes"
DEFAULT_BRANCH = "main"

#: Category directories in display order, with their human labels.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("breakfast", "Breakfast"),
    ("mains", "Mains"),
    ("sides", "Sides"),
    ("breads", "Breads"),
    ("desserts", "Desserts"),
    ("drinks", "Drinks"),
    ("basics", "Basics"),
)
CATEGORY_LABELS = dict(CATEGORIES)

#: Files in a category directory that are never recipes.
RESERVED_FILENAMES = {"TEMPLATE.md", "README.md"}

#: Frontmatter keys we understand. Anything else gets a warning.
KNOWN_KEYS = {
    "title",
    "servings",
    "prep_time",
    "cook_time",
    "total_time",
    "source",
    "tags",
    "image",
    "notes_in_jsonld",
    "nutrition",
}

#: Nutrition fields in display order: frontmatter key, label, schema.org
#: property, and the unit the number is written in. Everything but
#: `serving_size` is a bare number in the frontmatter; the unit is added here so
#: the recipe files stay free of `g`/`mg` noise.
NUTRITION_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("serving_size", "Serving size", "servingSize", ""),
    ("calories", "Calories", "calories", "calories"),
    ("protein", "Protein", "proteinContent", "g"),
    ("fat", "Fat", "fatContent", "g"),
    ("saturated_fat", "Saturated fat", "saturatedFatContent", "g"),
    ("carbs", "Carbohydrates", "carbohydrateContent", "g"),
    ("fiber", "Fiber", "fiberContent", "g"),
    ("sugar", "Sugar", "sugarContent", "g"),
    ("sodium", "Sodium", "sodiumContent", "mg"),
    ("cholesterol", "Cholesterol", "cholesterolContent", "mg"),
)
NUTRITION_KEYS = {key for key, _, _, _ in NUTRITION_FIELDS}

#: Heading names that mark the two structural sections.
INGREDIENT_HEADINGS = {"ingredients"}
INSTRUCTION_HEADINGS = {"instructions", "directions", "method", "steps"}

#: URL schemes allowed in Markdown links. Everything else renders as plain text.
ALLOWED_SCHEMES = {"http", "https", "mailto"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def esc(text: str) -> str:
    """Escape for HTML text and attribute contexts alike."""
    return html.escape(text, quote=True)


def slugify(text: str) -> str:
    """Lowercase kebab-case slug, used for section anchors and tag chips."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug.strip("-") or "section"


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int = 160) -> str:
    """Trim to a length suitable for a meta description, on a word boundary."""
    text = collapse(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.") + "…"


# --------------------------------------------------------------------------
# Parsed model
# --------------------------------------------------------------------------


@dataclass
class Item:
    """One parsed inline string, in both forms we need.

    ``html`` is display markup; ``text`` is the same content with the Markdown
    stripped and nothing escaped. JSON-LD and the search index always use
    ``text`` — a JSON string is not an HTML context, so entities and tags in
    there would be wrong twice over.
    """

    html: str
    text: str


@dataclass
class Block:
    """A chunk of an extra section: a subheading, a list, a paragraph, or code."""

    kind: str  # "subhead" | "list" | "para" | "code"
    item: Item | None = None
    items: list[Item] = field(default_factory=list)
    ordered: bool = False


@dataclass
class Group:
    """A run of ingredients or steps, optionally under a `###` subheading."""

    name: Item | None
    items: list[Item] = field(default_factory=list)


@dataclass
class Section:
    """A `##` section of a recipe body."""

    name: str  # heading text, plain
    name_item: Item
    kind: str  # "ingredients" | "instructions" | "extra"
    groups: list[Group] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.name)


@dataclass
class Duration:
    minutes: int
    iso: str
    display: str


@dataclass
class Nutrient:
    """One nutrition row, in the three forms the page needs."""

    label: str
    prop: str  # schema.org NutritionInformation property
    display: str  # "28 g", "320", "1 bowl"
    schema_value: str  # "28 g", "320 calories", "1 bowl"


@dataclass
class Recipe:
    path: Path
    rel_path: str
    category: str
    category_label: str
    slug: str
    title: str
    title_html: str
    description_blocks: list[Block]
    description_text: str
    ingredients: Section | None
    instructions: Section | None
    extras: list[Section]
    servings: str | None
    prep: Duration | None
    cook: Duration | None
    total: Duration | None
    source: str | None
    tags: list[str]
    image: str | None
    notes_in_jsonld: bool
    nutrition: list[Nutrient]
    date_published: str | None

    @property
    def id(self) -> str:
        return f"{self.category}/{self.slug}"

    @property
    def servings_display(self) -> str | None:
        """`4` means four servings; `12 cookies` already says what it means."""
        if not self.servings:
            return None
        if re.fullmatch(r"[\d\s./-]+", self.servings):
            return f"{self.servings.strip()} servings"
        return self.servings

    def ingredient_texts(self) -> list[str]:
        if not self.ingredients:
            return []
        return [item.text for group in self.ingredients.groups for item in group.items]


class ValidationError(Exception):
    """Raised for a problem that should fail the build after all files are read."""


# --------------------------------------------------------------------------
# Inline Markdown
# --------------------------------------------------------------------------

_CODE_RE = re.compile(r"(`+)(.+?)\1", re.S)
# The URL may contain one level of balanced parens, so `(javascript:alert(1))`
# is captured whole rather than leaving a stray `)` behind when it is rejected.
_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*((?:[^()\s]|\([^()]*\))*)(?:\s+[^)]*)?\)")
_STRONG_RE = re.compile(r"\*\*(\S.*?)\*\*", re.S)
_EM_STAR_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S)
_EM_UNDER_RE = re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", re.S)


def safe_link(url: str) -> str | None:
    """Return the URL if it is safe to put in an href, else None.

    Anything with an unexpected scheme (`javascript:`, `data:`, …) is rejected
    so a recipe file can never inject script through a link.
    """
    url = url.strip()
    if not url:
        return None
    if url.startswith("#"):
        return url
    if url.startswith("//"):
        # Protocol-relative. Cheap to reject, and no recipe needs one.
        return None
    parsed = urlparse(url)
    if parsed.scheme:
        return url if parsed.scheme.lower() in ALLOWED_SCHEMES else None
    return url


def _link_attrs(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() in ("http", "https"):
        return ' rel="noopener"'
    return ""


def render_inline(raw: str) -> Item:
    """Render one line of inline Markdown into display HTML and plain text.

    The ordering here matters. Code spans are pulled out *first*, before HTML
    escaping and before the emphasis and link passes, so that their contents are
    never reinterpreted: `` `**not bold**` `` has to stay literal, and a code
    span containing `<` must be escaped exactly once. The placeholders left
    behind use NUL bytes, which cannot occur in the source text.
    """
    codes: list[str] = []

    def stash(match: re.Match[str]) -> str:
        codes.append(match.group(2).strip())
        return f"\x00c{len(codes) - 1}\x00"

    stashed = _CODE_RE.sub(stash, raw)

    html_form = _inline_html(esc(stashed))
    text_form = _inline_text(stashed)

    for index, code in enumerate(codes):
        token = f"\x00c{index}\x00"
        html_form = html_form.replace(token, f"<code>{esc(code)}</code>")
        text_form = text_form.replace(token, code)

    return Item(html=html_form.strip(), text=collapse(text_form))


def _inline_html(text: str) -> str:
    def link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        # The URL has already been HTML-escaped along with everything else, so
        # `&` is `&amp;` — correct inside an attribute. Unescape only for the
        # scheme check.
        target = safe_link(html.unescape(url))
        if target is None:
            return label
        return f'<a href="{url}"{_link_attrs(target)}>{label}</a>'

    text = _LINK_RE.sub(link, text)
    text = _STRONG_RE.sub(r"<strong>\1</strong>", text)
    text = _EM_STAR_RE.sub(r"<em>\1</em>", text)
    text = _EM_UNDER_RE.sub(r"<em>\1</em>", text)
    return text


def _inline_text(text: str) -> str:
    text = _LINK_RE.sub(lambda m: m.group(1), text)
    text = _STRONG_RE.sub(r"\1", text)
    text = _EM_STAR_RE.sub(r"\1", text)
    text = _EM_UNDER_RE.sub(r"\1", text)
    return text


# --------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------

def split_frontmatter(text: str, rel_path: str) -> tuple[str, str]:
    """Split a `---` delimited frontmatter block off the front of the file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise ValidationError("frontmatter opened with --- but never closed")


def parse_frontmatter(block: str, rel_path: str) -> dict[str, object]:
    """Parse the frontmatter block as YAML.

    `safe_load` keeps this to plain data — no arbitrary tags — and gives back
    real types: `servings: 4` is an int, `tags: [...]` a list, `nutrition:` a
    mapping. The `as_*` helpers below normalise those back to what the rest of
    the build wants.
    """
    if not block.strip():
        return {}
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise ValidationError(f"frontmatter is not valid YAML: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError("frontmatter must be a mapping of keys to values")

    for key in data:
        if key not in KNOWN_KEYS:
            warn(f"{rel_path}: unknown frontmatter key {key!r} (ignored)")
    return data


def as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("false", "no", "off", "0"):
            return False
        if lowered in ("true", "yes", "on", "1"):
            return True
    return default


def as_text(value: object) -> str:
    """Scalar frontmatter value as a string.

    Containers mean the file said nothing useful in a scalar position, so they
    read as empty rather than as their repr. Booleans stringify as YAML wrote
    them, since `servings: yes` is a mistake worth seeing rather than `True`.
    """
    if value is None or isinstance(value, (list, dict)):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def as_list(value: object) -> list[str]:
    if value is None or isinstance(value, dict):
        return []
    if isinstance(value, list):
        return [as_text(entry) for entry in value if as_text(entry)]
    text = as_text(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def as_map(value: object) -> dict[str, str]:
    """Nested mapping frontmatter value. Anything else reads as empty."""
    if not isinstance(value, dict):
        return {}
    return {str(key): as_text(entry) for key, entry in value.items()}


# --------------------------------------------------------------------------
# Nutrition
# --------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]*)$")


def _format_number(raw: str) -> str:
    """`7.0` reads as `7`; `2.50` as `2.5`. Keeps the tables tidy."""
    number = float(raw)
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def parse_nutrition(value: object, rel_path: str) -> list[Nutrient]:
    """Build the nutrition rows from the `nutrition:` mapping.

    Values are bare numbers in the units named by NUTRITION_FIELDS; a written
    unit is accepted as long as it is the expected one, so `28` and `28 g` both
    work. `serving_size` is free text.
    """
    mapping = as_map(value)
    if not mapping:
        return []

    for key in mapping:
        if key not in NUTRITION_KEYS:
            warn(f"{rel_path}: unknown nutrition field {key!r} (ignored)")

    rows: list[Nutrient] = []
    for key, label, prop, unit in NUTRITION_FIELDS:
        raw = mapping.get(key, "").strip()
        if not raw:
            continue
        if not unit:  # serving_size
            rows.append(Nutrient(label, prop, raw, raw))
            continue
        match = _NUMBER_RE.match(raw.lower())
        if not match:
            raise ValidationError(
                f"nutrition.{key}: expected a number in {unit}, got {raw!r}"
            )
        written = match.group(2)
        if written and written not in (unit, unit.rstrip("s"), "kcal"):
            raise ValidationError(
                f"nutrition.{key}: expected {unit}, got {raw!r}"
            )
        number = _format_number(match.group(1))
        display = number if unit == "calories" else f"{number} {unit}"
        rows.append(Nutrient(label, prop, display, f"{number} {unit}"))
    return rows


# --------------------------------------------------------------------------
# Durations
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]*)")
_DAY_UNITS = {"d", "day", "days"}
_HOUR_UNITS = {"h", "hr", "hrs", "hour", "hours"}
_MINUTE_UNITS = {"", "m", "min", "mins", "minute", "minutes"}


def parse_duration(raw: str, rel_path: str, fieldname: str) -> Duration:
    """Parse the durations people actually write into a Duration.

    Accepts `15m`, `1h30m`, `1h 30m`, `90 min`, `1 hr 5 min`,
    `1 hour 30 minutes`, and a bare number (minutes). Anything else is a
    validation error rather than a silent zero.
    """
    text = raw.strip().lower()
    if not text:
        raise ValidationError(f"{fieldname}: empty duration")

    normalised = re.sub(r"[,+]|\band\b", " ", text)
    normalised = re.sub(r"(?<=[a-z])\.", " ", normalised)  # "1 hr." reads as "1 hr"
    total = 0.0
    position = 0
    seen = False

    while position < len(normalised):
        if normalised[position].isspace():
            position += 1
            continue
        match = _TOKEN_RE.match(normalised, position)
        if not match:
            raise ValidationError(f"{fieldname}: cannot parse duration {raw.strip()!r}")
        amount, unit = float(match.group(1)), match.group(2)
        if unit in _MINUTE_UNITS:
            total += amount
        elif unit in _HOUR_UNITS:
            total += amount * 60
        elif unit in _DAY_UNITS:
            total += amount * 60 * 24
        else:
            raise ValidationError(
                f"{fieldname}: unknown time unit {unit!r} in {raw.strip()!r}"
            )
        seen = True
        position = match.end()

    if not seen:
        raise ValidationError(f"{fieldname}: cannot parse duration {raw.strip()!r}")
    return duration_from_minutes(int(round(total)))


#: Durations named in step prose: `5 min`, `1–2 min`, `20–25 min`, `2 to 4
#: hours`, `about 45 seconds`. Only the first number of a range is captured —
#: the low end is when you want to look at the food.
_STEP_TIMER_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:[–—-]|to|or)?\s*(?:\d+(?:\.\d+)?)?\s*"
    r"(secs?|seconds?|mins?|minutes?|hrs?|hours?)\b",
    re.IGNORECASE,
)

#: Anything longer is a marinade or a cure, not something to stand around for.
MAX_TIMER_SECONDS = 8 * 60 * 60


def timer_label(seconds: int) -> str:
    """`45 sec`, `20 min`, `1 hr 30 min` — how the button reads."""
    if seconds < 60:
        return f"{seconds} sec"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hr" if not minutes else f"{hours} hr {minutes} min"


def find_step_timers(text: str) -> list[tuple[int, str]]:
    """Every duration a step names, as (seconds, label), in the order written.

    Deliberately only seconds, minutes and hours: days and weeks in a recipe are
    storage advice ("keeps 3 weeks refrigerated"), never a countdown.
    """
    found: list[tuple[int, str]] = []
    seen: set[int] = set()

    for match in _STEP_TIMER_RE.finditer(text):
        amount, unit = float(match.group(1)), match.group(2).lower()
        if unit.startswith("s"):
            seconds = int(round(amount))
        elif unit.startswith("m"):
            seconds = int(round(amount * 60))
        else:
            seconds = int(round(amount * 3600))

        if not 5 <= seconds <= MAX_TIMER_SECONDS or seconds in seen:
            continue
        seen.add(seconds)
        found.append((seconds, timer_label(seconds)))

    return found


def duration_from_minutes(minutes: int) -> Duration:
    hours, mins = divmod(max(minutes, 0), 60)
    iso = "PT" + (f"{hours}H" if hours else "") + (f"{mins}M" if mins or not hours else "")
    parts = []
    if hours:
        parts.append(f"{hours} hr")
    if mins or not hours:
        parts.append(f"{mins} min")
    return Duration(minutes=minutes, iso=iso, display=" ".join(parts))


# --------------------------------------------------------------------------
# Body parsing
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_H1_RE = re.compile(r"^#(?!#)\s+(.*)$")
_H2_RE = re.compile(r"^##(?!#)\s+(.*)$")
_H3_RE = re.compile(r"^###(?!#)\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")


def split_document(body: str) -> tuple[str | None, list[str], list[tuple[str, list[str]]]]:
    """Split a body into (H1 text, pre-section lines, [(heading, lines)]).

    Fenced code is tracked here so a `# comment` inside a shell snippet is never
    mistaken for a heading.
    """
    h1: str | None = None
    lead: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fence: str | None = None

    for raw in body.splitlines():
        if fence is None:
            fence_match = _FENCE_RE.match(raw)
            if fence_match:
                fence = fence_match.group(1)
            else:
                if h1 is None:
                    heading = _H1_RE.match(raw)
                    if heading:
                        h1 = heading.group(1).strip()
                        continue
                heading2 = _H2_RE.match(raw)
                if heading2:
                    sections.append((heading2.group(1).strip(), []))
                    continue
        elif raw.strip().startswith(fence):
            fence = None
        (sections[-1][1] if sections else lead).append(raw)

    return h1, lead, sections


def parse_blocks(lines: list[str]) -> list[Block]:
    """Turn a run of body lines into ordered subhead / list / paragraph blocks.

    List items may wrap: a following line that is indented, or that follows with
    no blank line between, continues the item rather than starting a paragraph.
    """
    blocks: list[Block] = []
    chunks: list[list[str]] | None = None  # open list, one str list per item
    ordered = False
    para: list[str] = []
    code: list[str] | None = None
    fence: str | None = None
    prev_blank = False

    def flush_list() -> None:
        nonlocal chunks
        if chunks:
            items = [render_inline(" ".join(chunk)) for chunk in chunks]
            blocks.append(Block("list", items=items, ordered=ordered))
        chunks = None

    def flush_para() -> None:
        nonlocal para
        if para:
            blocks.append(Block("para", item=render_inline(" ".join(para))))
        para = []

    for raw in lines:
        line = raw.rstrip()
        fence_match = _FENCE_RE.match(line)

        if fence is not None:
            assert code is not None
            if fence_match and line.strip().startswith(fence):
                text = "\n".join(code)
                blocks.append(Block("code", item=Item(html=esc(text), text=text)))
                fence, code = None, None
            else:
                code.append(raw)
            continue

        if fence_match:
            flush_list()
            flush_para()
            fence, code = fence_match.group(1), []
            continue

        if not line.strip():
            flush_para()
            prev_blank = True
            continue

        subhead = _H3_RE.match(line)
        if subhead:
            flush_list()
            flush_para()
            blocks.append(Block("subhead", item=render_inline(subhead.group(1).strip())))
            prev_blank = False
            continue

        bullet = _BULLET_RE.match(line)
        ordered_item = None if bullet else _ORDERED_RE.match(line)
        if bullet or ordered_item:
            flush_para()
            if chunks is None:
                chunks = []
                ordered = ordered_item is not None
            chunks.append([(bullet or ordered_item).group(2).strip()])
            prev_blank = False
            continue

        indented = raw[:1] in (" ", "\t")
        if chunks and (not prev_blank or indented):
            chunks[-1].append(line.strip())
        else:
            flush_list()
            para.append(line.strip())
        prev_blank = False

    if fence is not None and code:
        text = "\n".join(code)
        blocks.append(Block("code", item=Item(html=esc(text), text=text)))
    flush_list()
    flush_para()
    return blocks


def blocks_to_groups(blocks: list[Block]) -> list[Group]:
    """Fold blocks into ingredient/step groups keyed on `###` subheadings."""
    groups: list[Group] = [Group(name=None)]
    for block in blocks:
        if block.kind == "subhead":
            groups.append(Group(name=block.item))
        elif block.kind == "list":
            groups[-1].items.extend(block.items)
        elif block.kind == "para" and block.item is not None:
            # A stray paragraph in a structural section is still an entry.
            groups[-1].items.append(block.item)
    return [group for group in groups if group.items or group.name is not None]


def classify(heading: str) -> str:
    lowered = heading.strip().lower()
    if lowered in INGREDIENT_HEADINGS:
        return "ingredients"
    if lowered in INSTRUCTION_HEADINGS:
        return "instructions"
    return "extra"


# --------------------------------------------------------------------------
# Loading a recipe
# --------------------------------------------------------------------------


def git_date(path: Path) -> str | None:
    """First commit date of a file, ISO-8601, or None if git cannot tell us."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "--", str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def load_recipe(path: Path, category: str, errors: list[str]) -> Recipe | None:
    """Parse one Markdown file. Records errors instead of raising where it can."""
    try:
        rel_path = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:  # a file from outside the repo; report what we can
        rel_path = path.as_posix()
    text = path.read_text(encoding="utf-8")

    def fail(message: str) -> None:
        errors.append(f"{rel_path}: {message}")

    try:
        front_block, body = split_frontmatter(text, rel_path)
    except ValidationError as exc:
        fail(str(exc))
        return None

    meta = parse_frontmatter(front_block, rel_path)
    h1, lead, raw_sections = split_document(body)

    title = as_text(meta.get("title")) or (h1 or "").strip()
    if not title:
        fail("missing title (frontmatter `title:` or a `# Heading`)")

    title_item = render_inline(h1) if h1 else render_inline(title)

    description_blocks = parse_blocks(lead)
    description_text = collapse(
        " ".join(
            block.item.text
            for block in description_blocks
            if block.kind == "para" and block.item is not None
        )
    )

    ingredients: Section | None = None
    instructions: Section | None = None
    extras: list[Section] = []

    for heading, lines in raw_sections:
        kind = classify(heading)
        blocks = parse_blocks(lines)
        section = Section(
            name=heading,
            name_item=render_inline(heading),
            kind=kind,
            groups=blocks_to_groups(blocks) if kind != "extra" else [],
            blocks=blocks if kind == "extra" else [],
        )
        if kind == "ingredients" and ingredients is None:
            ingredients = section
        elif kind == "instructions" and instructions is None:
            instructions = section
        else:
            # A second Ingredients/Instructions section is unusual but harmless;
            # keep it as an extra section rather than dropping content.
            if kind != "extra":
                section.kind = "extra"
                section.blocks = blocks
                section.groups = []
            extras.append(section)

    if ingredients is None:
        fail("missing `## Ingredients` section")
    elif not any(group.items for group in ingredients.groups):
        fail("`## Ingredients` section is empty")

    if instructions is None:
        fail("missing `## Instructions` section")
    elif not any(group.items for group in instructions.groups):
        fail(f"`## {instructions.name}` section is empty")

    def duration(key: str) -> Duration | None:
        raw = as_text(meta.get(key))
        if not raw:
            return None
        try:
            return parse_duration(raw, rel_path, key)
        except ValidationError as exc:
            fail(str(exc))
            return None

    prep = duration("prep_time")
    cook = duration("cook_time")
    total = duration("total_time")
    if total is None and (prep or cook):
        total = duration_from_minutes(
            (prep.minutes if prep else 0) + (cook.minutes if cook else 0)
        )

    servings = as_text(meta.get("servings")) or None
    source = as_text(meta.get("source")) or None
    image = as_text(meta.get("image")) or None

    try:
        nutrition = parse_nutrition(meta.get("nutrition"), rel_path)
    except ValidationError as exc:
        fail(str(exc))
        nutrition = []

    return Recipe(
        path=path,
        rel_path=rel_path,
        category=category,
        category_label=CATEGORY_LABELS[category],
        slug=path.stem,
        title=title,
        title_html=title_item.html or esc(title),
        description_blocks=description_blocks,
        description_text=description_text,
        ingredients=ingredients,
        instructions=instructions,
        extras=extras,
        servings=servings,
        prep=prep,
        cook=cook,
        total=total,
        source=source,
        tags=as_list(meta.get("tags")),
        image=image,
        notes_in_jsonld=as_bool(meta.get("notes_in_jsonld"), default=True),
        nutrition=nutrition,
        date_published=git_date(path),
    )


def discover(errors: list[str]) -> list[Recipe]:
    """Find every recipe, in category order and alphabetical by title within one."""
    recipes: list[Recipe] = []
    for category, _label in CATEGORIES:
        directory = REPO_ROOT / category
        if not directory.is_dir():
            continue
        found: list[Recipe] = []
        for path in sorted(directory.rglob("*.md")):
            if path.name in RESERVED_FILENAMES:
                continue
            recipe = load_recipe(path, category, errors)
            if recipe is not None:
                found.append(recipe)
        found.sort(key=lambda item: item.title.lower())
        recipes.extend(found)

    seen: dict[str, Recipe] = {}
    for recipe in recipes:
        if recipe.id in seen:
            errors.append(
                f"{recipe.rel_path}: duplicate slug {recipe.id!r} "
                f"(already used by {seen[recipe.id].rel_path})"
            )
        else:
            seen[recipe.id] = recipe
    return recipes


# --------------------------------------------------------------------------
# HTML fragments
# --------------------------------------------------------------------------


def render_blocks(blocks: list[Block], list_class: str, indent: str = "    ") -> str:
    """Render extra-section (or lede) blocks, preserving document order."""
    out: list[str] = []
    for block in blocks:
        if block.kind == "subhead" and block.item is not None:
            out.append(f'{indent}<h3 class="group-title">{block.item.html}</h3>')
        elif block.kind == "para" and block.item is not None:
            out.append(f"{indent}<p>{block.item.html}</p>")
        elif block.kind == "code" and block.item is not None:
            out.append(f"{indent}<pre><code>{block.item.html}</code></pre>")
        elif block.kind == "list":
            tag = "ol" if block.ordered else "ul"
            items = "".join(f"<li>{item.html}</li>" for item in block.items)
            out.append(f'{indent}<{tag} class="{list_class}">{items}</{tag}>')
    return "\n".join(out)


def render_ingredients(recipe: Recipe) -> str:
    section = recipe.ingredients
    assert section is not None
    out = [
        '    <section class="ingredients" aria-labelledby="ingredients-h">',
        f'      <h2 id="ingredients-h">{section.name_item.html}</h2>',
    ]
    for group in section.groups:
        out.append('      <div class="ing-group">')
        if group.name is not None:
            out.append(f'        <h3 class="group-title">{group.name.html}</h3>')
        out.append('        <ul class="ing-list">')
        for item in group.items:
            out.append(
                # Disabled until cooking mode enables it: the checkbox is hidden
                # in the ordinary view, and a hidden control that still toggles
                # on a label click would strike ingredients out invisibly.
                "          <li><label><input type=\"checkbox\" disabled>"
                f'<span itemprop="recipeIngredient">{item.html}</span></label></li>'
            )
        out.append("        </ul>")
        out.append("      </div>")
    out.append("    </section>")
    return "\n".join(out)


def render_step_timers(text: str) -> str:
    """Timer buttons for the durations a step names, or nothing.

    Ships `hidden`: the buttons do nothing without JavaScript, and they only
    belong on screen in cooking mode. They sit outside the `itemprop="text"`
    span so the machine-readable step text stays exactly the prose.
    """
    timers = find_step_timers(text)
    if not timers:
        return ""
    buttons = "".join(
        f'<button type="button" class="step-timer" data-seconds="{seconds}">'
        f"{esc(label)}</button>"
        for seconds, label in timers
    )
    return f'<span class="step-timers" hidden>{buttons}</span>'


def render_instructions(recipe: Recipe) -> str:
    section = recipe.instructions
    assert section is not None
    out = [
        '    <section class="instructions" aria-labelledby="instructions-h">',
        f'      <h2 id="instructions-h">{section.name_item.html}</h2>',
    ]
    for group in section.groups:
        if group.name is not None:
            out.append(f'      <h3 class="group-title">{group.name.html}</h3>')
        out.append('      <ol class="step-list">')
        for item in group.items:
            out.append(
                '        <li class="step" itemprop="recipeInstructions" itemscope '
                'itemtype="https://schema.org/HowToStep">'
                f'<span itemprop="text">{item.html}</span>'
                f"{render_step_timers(item.text)}</li>"
            )
        out.append("      </ol>")
    out.append("    </section>")
    return "\n".join(out)


def render_extras(recipe: Recipe) -> str:
    out: list[str] = []
    for section in recipe.extras:
        out.append(f'  <section class="extra" id="{esc(section.slug)}">')
        out.append(f"    <h2>{section.name_item.html}</h2>")
        body = render_blocks(section.blocks, "note-list")
        if body:
            out.append(body)
        out.append("  </section>")
    return "\n".join(out)


def render_nutrition(recipe: Recipe) -> str:
    """The visible nutrition table. Microdata for it lives in render_microdata."""
    if not recipe.nutrition:
        return ""
    out = [
        '  <section class="nutrition" id="nutrition" aria-labelledby="nutrition-h">',
        '    <h2 id="nutrition-h">Nutrition</h2>',
        '    <dl class="nutrition-list">',
    ]
    for row in recipe.nutrition:
        out.append(
            f'      <div class="nutrient"><dt>{esc(row.label)}</dt>'
            f"<dd>{esc(row.display)}</dd></div>"
        )
    out.append("    </dl>")
    out.append(
        '    <p class="nutrition-note">Estimated from the ingredient list, '
        "per serving. Not a substitute for a label.</p>"
    )
    out.append("  </section>")
    return "\n".join(out)


def render_meta_list(recipe: Recipe) -> str:
    rows: list[tuple[str, str]] = []
    if recipe.servings:
        rows.append(
            ("Serves", f'<dd itemprop="recipeYield">{esc(recipe.servings)}</dd>')
        )
    for label, value in (
        ("Prep", recipe.prep),
        ("Cook", recipe.cook),
        ("Total", recipe.total),
    ):
        if value is not None:
            rows.append((label, f"<dd>{esc(value.display)}</dd>"))
    return "".join(
        f'<div class="meta-item"><dt>{label}</dt>{cell}</div>' for label, cell in rows
    )


def render_tags(tags: list[str]) -> str:
    return "".join(f"<li>{esc(tag)}</li>" for tag in tags)


def render_footer(recipe: Recipe, repository: str, branch: str) -> str:
    """Source attribution plus a link back to the Markdown file on GitHub."""
    out: list[str] = []
    source = recipe.source
    if source:
        if source.strip().lower() in ("original", "“original”", '"original"'):
            out.append('    <p class="source">Original recipe.</p>')
        else:
            parsed = urlparse(source)
            if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
                label = esc(parsed.netloc.removeprefix("www."))
                out.append(
                    f'    <p class="source">Adapted from '
                    f'<a href="{esc(source)}" rel="noopener">{label}</a>.</p>'
                )
            else:
                out.append(
                    f'    <p class="source">Adapted from {render_inline(source).html}.</p>'
                )
    blob = f"https://github.com/{repository}/blob/{branch}/{recipe.rel_path}"
    out.append(
        f'    <p class="source-link"><a href="{esc(blob)}" rel="noopener">'
        "View the Markdown source</a></p>"
    )
    return "\n".join(out)


def render_microdata(recipe: Recipe, canonical: str, image_url: str | None) -> str:
    """Hidden `<meta itemprop>` tags for the values the visible page does not carry."""
    out = [f'  <meta itemprop="url" content="{esc(canonical)}">']
    out.append(f'  <meta itemprop="recipeCategory" content="{esc(recipe.category_label)}">')
    for prop, value in (
        ("prepTime", recipe.prep),
        ("cookTime", recipe.cook),
        ("totalTime", recipe.total),
    ):
        if value is not None:
            out.append(f'  <meta itemprop="{prop}" content="{value.iso}">')
    if recipe.tags:
        out.append(f'  <meta itemprop="keywords" content="{esc(", ".join(recipe.tags))}">')
    if recipe.date_published:
        out.append(
            f'  <meta itemprop="datePublished" content="{esc(recipe.date_published)}">'
        )
    if image_url:
        out.append(f'  <meta itemprop="image" content="{esc(image_url)}">')
    out.append(
        '  <div itemprop="author" itemscope itemtype="https://schema.org/Person" hidden>'
        f'<meta itemprop="name" content="{esc(AUTHOR_NAME)}"></div>'
    )
    if recipe.nutrition:
        # The visible table carries the numbers without their units, so the
        # microdata values live here rather than on the rendered rows.
        out.append(
            '  <div itemprop="nutrition" itemscope '
            'itemtype="https://schema.org/NutritionInformation" hidden>'
        )
        for row in recipe.nutrition:
            out.append(
                f'    <meta itemprop="{row.prop}" content="{esc(row.schema_value)}">'
            )
        out.append("  </div>")
    return "\n".join(out)


def render_cards(recipes: list[Recipe], base: str) -> str:
    """Category sections of cards. These render fine with JavaScript disabled."""
    out: list[str] = []
    for category, label in CATEGORIES:
        in_category = [recipe for recipe in recipes if recipe.category == category]
        if not in_category:
            continue
        out.append(f'<section class="category" id="{category}" data-category="{category}">')
        out.append(f'  <h2 class="category-title">{esc(label)}</h2>')
        out.append('  <ul class="card-grid">')
        for recipe in in_category:
            url = f"{base}{recipe.category}/{recipe.slug}/"
            out.append(f'    <li class="card" data-id="{esc(recipe.id)}">')
            out.append(f'      <a class="card-link" href="{esc(url)}">')
            out.append(f'        <h3 class="card-title">{esc(recipe.title)}</h3>')
            if recipe.description_text:
                out.append(
                    f'        <p class="card-desc">{esc(truncate(recipe.description_text, 180))}</p>'
                )
            meta = []
            if recipe.servings_display:
                meta.append(f"<span>{esc(recipe.servings_display)}</span>")
            if recipe.total is not None:
                meta.append(f"<span>{esc(recipe.total.display)}</span>")
            if meta:
                out.append(f'        <p class="card-meta">{"".join(meta)}</p>')
            if recipe.tags:
                out.append(
                    f'        <ul class="card-tags">{render_tags(recipe.tags)}</ul>'
                )
            out.append("      </a>")
            out.append("    </li>")
        out.append("  </ul>")
        out.append("</section>")
    return "\n".join(out)


def render_search_block(recipes: list[Recipe], count_label: str) -> str:
    """The search form, tag chips and empty state.

    Everything here is `hidden` in the markup; `search.js` unhides it, so a
    browser without JavaScript never shows controls that would not work.
    """
    tags = sorted({tag for recipe in recipes for tag in recipe.tags}, key=str.lower)
    chips = "\n".join(
        f'  <button class="chip" type="button" data-tag="{esc(tag)}">{esc(tag)}</button>'
        for tag in tags
    )
    return "\n".join(
        [
            '<form class="search" role="search" hidden>',
            '  <input type="search" id="q" placeholder="Search recipes…" autocomplete="off">',
            f'  <p class="result-count" aria-live="polite">{esc(count_label)}</p>',
            "</form>",
            "",
            '<div class="tag-filters" hidden>',
            chips,
            "</div>",
            "",
            '<p class="empty-state" hidden>No recipes match that search.</p>',
        ]
    )


# --------------------------------------------------------------------------
# JSON-LD
# --------------------------------------------------------------------------


def how_to_step(text: str) -> dict[str, str]:
    return {"@type": "HowToStep", "text": text}


def extra_section_steps(section: Section) -> list[dict[str, str]]:
    """Bullets and paragraphs of an extra section, as HowToStep entries."""
    steps: list[dict[str, str]] = []
    for block in section.blocks:
        if block.kind == "list":
            steps.extend(how_to_step(item.text) for item in block.items)
        elif block.kind == "para" and block.item is not None:
            steps.append(how_to_step(block.item.text))
    return steps


def build_jsonld(recipe: Recipe, canonical: str, image_url: str | None) -> dict[str, object]:
    data: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": recipe.title,
    }
    if recipe.description_text:
        data["description"] = recipe.description_text
    data["url"] = canonical
    data["author"] = {"@type": "Person", "name": AUTHOR_NAME}
    data["recipeCategory"] = recipe.category_label
    if recipe.servings_display:
        data["recipeYield"] = recipe.servings_display
    if recipe.prep is not None:
        data["prepTime"] = recipe.prep.iso
    if recipe.cook is not None:
        data["cookTime"] = recipe.cook.iso
    if recipe.total is not None:
        data["totalTime"] = recipe.total.iso
    if recipe.tags:
        data["keywords"] = ", ".join(recipe.tags)
    if image_url:
        data["image"] = image_url
    data["recipeIngredient"] = recipe.ingredient_texts()

    groups = recipe.instructions.groups if recipe.instructions else []
    extras = recipe.extras if recipe.notes_in_jsonld else []
    heading = recipe.instructions.name if recipe.instructions else "Instructions"

    # A single unnamed group with nothing else to say is best expressed as a
    # plain list of steps; consumers that only understand flat instructions get
    # the whole recipe. As soon as there are named groups or extra sections,
    # everything has to become a HowToSection so the grouping is not lost.
    if len(groups) == 1 and groups[0].name is None and not extras:
        data["recipeInstructions"] = [how_to_step(item.text) for item in groups[0].items]
    else:
        sections: list[dict[str, object]] = []
        for group in groups:
            sections.append(
                {
                    "@type": "HowToSection",
                    "name": group.name.text if group.name is not None else heading,
                    "itemListElement": [how_to_step(item.text) for item in group.items],
                }
            )
        for extra in extras:
            steps = extra_section_steps(extra)
            if steps:
                sections.append(
                    {
                        "@type": "HowToSection",
                        "name": extra.name_item.text,
                        "itemListElement": steps,
                    }
                )
        data["recipeInstructions"] = sections

    if recipe.nutrition:
        nutrition: dict[str, object] = {"@type": "NutritionInformation"}
        for row in recipe.nutrition:
            nutrition[row.prop] = row.schema_value
        data["nutrition"] = nutrition

    if recipe.date_published:
        data["datePublished"] = recipe.date_published
    return data


def dump_json(data: object) -> str:
    """Serialise for embedding in a `<script>` element.

    `</` is escaped to `<\\/` because a literal `</script>` anywhere inside the
    element — even in a JSON string — ends the script early. `<\\/` is a legal
    JSON escape for `/`, so parsers see identical data.
    """
    return json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")


# --------------------------------------------------------------------------
# Page assembly
# --------------------------------------------------------------------------


def social_meta(
    *,
    title: str,
    description: str,
    url: str,
    kind: str,
    image: str | None,
) -> str:
    tags = [
        f'<meta property="og:type" content="{kind}">',
        f'<meta property="og:title" content="{esc(title)}">',
        f'<meta property="og:description" content="{esc(description)}">',
        f'<meta property="og:url" content="{esc(url)}">',
        f'<meta property="og:site_name" content="{esc(SITE_TITLE)}">',
        f'<meta name="twitter:card" content="{"summary_large_image" if image else "summary"}">',
        f'<meta name="twitter:title" content="{esc(title)}">',
        f'<meta name="twitter:description" content="{esc(description)}">',
    ]
    if image:
        tags.append(f'<meta property="og:image" content="{esc(image)}">')
        tags.append(f'<meta name="twitter:image" content="{esc(image)}">')
    return "\n".join(tags)


class Site:
    """Holds the build settings and the loaded templates."""

    def __init__(self, out: Path, base: str, site_url: str, repository: str, branch: str):
        self.out = out
        self.base = base
        self.site_url = site_url
        self.repository = repository
        self.branch = branch
        templates = REPO_ROOT / "site" / "templates"
        self.base_tmpl = self._load(templates / "base.html")
        self.index_tmpl = self._load(templates / "index.html")
        self.recipe_tmpl = self._load(templates / "recipe.html")

    @staticmethod
    def _load(path: Path) -> Template:
        if not path.is_file():
            raise SystemExit(f"error: missing template {path}")
        return Template(path.read_text(encoding="utf-8"))

    def page_url(self, recipe: Recipe) -> str:
        return f"{self.base}{recipe.category}/{recipe.slug}/"

    def canonical(self, recipe: Recipe) -> str:
        return urljoin(self.site_url, f"{recipe.category}/{recipe.slug}/")

    def absolute(self, url: str) -> str:
        return urljoin(self.site_url, url)

    def shell(self, **values: str) -> str:
        defaults = {
            "site_title": SITE_TITLE,
            "base": self.base,
            "nav": "",
            "scripts": "",
            "head_extra": "",
            "content": "",
        }
        defaults.update(values)
        return self.base_tmpl.safe_substitute(defaults)


def render_home(site: Site, recipes: list[Recipe], index_json: str) -> str:
    count_label = f"{len(recipes)} recipe" + ("" if len(recipes) == 1 else "s")
    content = site.index_tmpl.safe_substitute(
        {
            "search": render_search_block(recipes, count_label),
            "sections": render_cards(recipes, site.base),
            "count": count_label,
            "search_index_json": index_json,
        }
    )
    return site.shell(
        title=esc(SITE_TITLE),
        meta_description=esc(SITE_DESCRIPTION),
        canonical=esc(site.site_url),
        body_class="home",
        head_extra=social_meta(
            title=SITE_TITLE,
            description=SITE_DESCRIPTION,
            url=site.site_url,
            kind="website",
            image=None,
        ),
        content=content,
        scripts=f'<script src="{esc(site.base)}assets/search.js" defer></script>',
    )


def render_recipe_page(site: Site, recipe: Recipe) -> str:
    canonical = site.canonical(recipe)
    image_url = site.absolute(recipe.image) if recipe.image else None
    description = recipe.description_text or f"{recipe.title}, from a personal recipe collection."

    jsonld = (
        '<script type="application/ld+json">\n'
        + dump_json(build_jsonld(recipe, canonical, image_url))
        + "\n</script>"
    )

    content = site.recipe_tmpl.safe_substitute(
        {
            "eyebrow": f'<a href="{esc(site.base)}#{esc(recipe.category)}">'
            f"{esc(recipe.category_label)}</a>",
            "title_html": recipe.title_html,
            # No indent: the template puts the lede on one line inside its div.
            "description_html": render_blocks(recipe.description_blocks, "lede-list", ""),
            "meta_list": render_meta_list(recipe),
            "tags": render_tags(recipe.tags),
            "ingredients": render_ingredients(recipe),
            "instructions": render_instructions(recipe),
            "extra_sections": render_extras(recipe),
            "nutrition": render_nutrition(recipe),
            "footer": render_footer(recipe, site.repository, site.branch),
            "canonical": esc(canonical),
            "microdata_meta": render_microdata(recipe, canonical, image_url),
        }
    )

    return site.shell(
        title=esc(f"{recipe.title} — {SITE_TITLE}"),
        meta_description=esc(truncate(description)),
        canonical=esc(canonical),
        body_class="recipe-page",
        head_extra=jsonld
        + "\n"
        + social_meta(
            title=recipe.title,
            description=truncate(description),
            url=canonical,
            kind="article",
            image=image_url,
        ),
        content=content,
        nav=f'<a class="back-link" href="{esc(site.base)}">All recipes</a>',
        scripts=(
            f'<script src="{esc(site.base)}assets/recipe.js" defer></script>\n'
            f'<script src="{esc(site.base)}assets/cooking.js" defer></script>'
        ),
    )


def build_search_index(site: Site, recipes: list[Recipe]) -> list[dict[str, object]]:
    entries = []
    for recipe in recipes:
        entries.append(
            {
                "id": recipe.id,
                "title": recipe.title,
                "url": site.page_url(recipe),
                "category": recipe.category,
                "categoryLabel": recipe.category_label,
                "description": recipe.description_text,
                "tags": recipe.tags,
                "ingredients": recipe.ingredient_texts(),
                "time": recipe.total.display if recipe.total else "",
                "servings": recipe.servings_display or "",
            }
        )
    return entries


# --------------------------------------------------------------------------
# Progressive web app
# --------------------------------------------------------------------------

#: Manifest icons, as (asset path, size, purpose).
MANIFEST_ICONS: tuple[tuple[str, str, str], ...] = (
    ("assets/icon-192.png", "192x192", "any"),
    ("assets/icon-512.png", "512x512", "any"),
    ("assets/icon-maskable-512.png", "512x512", "maskable"),
)

THEME_COLOR = "#17140f"
BACKGROUND_COLOR = "#faf7f2"

#: Written to the site root. `$version` changes whenever any built file does,
#: which is what makes a deploy replace the old cache instead of stranding an
#: installed copy on stale pages.
SERVICE_WORKER = Template(
    """\
// Generated by build.py. Edit the template there, not this file.
//
// Cache-first with a background refresh: an installed copy opens instantly and
// works with no signal, and each visit quietly re-fetches what it served. A new
// build changes VERSION, so the install step below replaces the whole cache.

const VERSION = '$version';
const CACHE = 'recipes-' + VERSION;
const SCOPE = '$base';
const START_URL = '$base';
const PRECACHE = $urls;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(SCOPE)) {
    return;
  }

  event.respondWith(
    caches.match(request, { ignoreSearch: true }).then((hit) => {
      const fresh = fetch(request)
        .then((response) => {
          if (response && response.ok && response.type === 'basic') {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => {
          // Offline. A navigation with nothing cached still gets the home page,
          // which is enough to reach every recipe already in the cache.
          if (hit) {
            return hit;
          }
          return request.mode === 'navigate' ? caches.match(START_URL) : Response.error();
        });

      return hit || fresh;
    })
  );
});

// Cooking mode posts its timer alarms through this registration, because
// `new Notification()` throws on Android Chrome. Tapping one should land on the
// recipe that set it rather than opening a second copy of the site.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = data.url || START_URL;

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url === target && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.length && 'focus' in clients[0]) {
        return clients[0].focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
"""
)


def build_manifest(site: Site) -> dict[str, object]:
    return {
        "name": f"{SITE_TITLE} — {AUTHOR_NAME}",
        "short_name": SITE_TITLE,
        "description": SITE_DESCRIPTION,
        "id": site.base,
        "start_url": site.base,
        "scope": site.base,
        "display": "standalone",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "lang": "en",
        "categories": ["food", "lifestyle"],
        "icons": [
            {
                "src": f"{site.base}{path}",
                "sizes": sizes,
                "type": "image/png",
                "purpose": purpose,
            }
            for path, sizes, purpose in MANIFEST_ICONS
        ],
    }


def precache_urls(site: Site) -> tuple[list[str], str]:
    """Every built file as a URL, plus a version stamp over their contents.

    Walks the output directory rather than tracking writes, so anything the
    build produces — pages, assets, the search index — is offline by default.
    """
    entries: list[tuple[str, bytes]] = []
    for path in sorted(site.out.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(site.out)
        if relative.name in ("sw.js", ".nojekyll"):
            continue
        if relative.name == "index.html":
            url = site.base + relative.parent.as_posix().removeprefix(".").removeprefix("/")
            if not url.endswith("/"):
                url += "/"
        else:
            url = site.base + relative.as_posix()
        entries.append((url, path.read_bytes()))

    digest = hashlib.sha256()
    for url, payload in entries:
        digest.update(url.encode("utf-8"))
        digest.update(hashlib.sha256(payload).digest())

    return [url for url, _ in entries], digest.hexdigest()[:16]


def render_service_worker(site: Site) -> str:
    urls, version = precache_urls(site)
    return SERVICE_WORKER.substitute(
        version=version,
        base=site.base,
        urls=json.dumps(urls, indent=2),
    )


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def prepare_out(out: Path) -> Path:
    """Empty and recreate the output directory, with guards against disasters."""
    resolved = out.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home():
        raise SystemExit(f"error: refusing to clean {resolved}")
    if resolved == REPO_ROOT or resolved in REPO_ROOT.parents:
        raise SystemExit(f"error: refusing to clean {resolved} (contains the repository)")
    if resolved.exists() and not resolved.is_dir():
        raise SystemExit(f"error: output path {resolved} is not a directory")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)
    return resolved


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def copy_static(out: Path) -> None:
    static = REPO_ROOT / "site" / "static"
    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    if not static.is_dir():
        warn(f"no static directory at {static}; the site will have no CSS")
        return
    for entry in sorted(static.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            shutil.copytree(entry, assets / entry.name, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, assets / entry.name)


def build(site: Site, recipes: list[Recipe]) -> None:
    out = site.out
    index_json = dump_json(build_search_index(site, recipes))

    write(out / "index.html", render_home(site, recipes, index_json))
    write(out / "search-index.json", index_json + "\n")
    write(out / ".nojekyll", "")

    for recipe in recipes:
        write(out / recipe.category / recipe.slug / "index.html", render_recipe_page(site, recipe))

    copy_static(out)

    # Last, so the precache list and version cover everything above.
    write(out / "manifest.webmanifest", dump_json(build_manifest(site)) + "\n")
    write(out / "sw.js", render_service_worker(site))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def normalise_base(value: str) -> str:
    value = value.strip()
    if not value:
        return "/"
    if not value.startswith("/"):
        value = "/" + value
    if not value.endswith("/"):
        value = value + "/"
    return value


def default_locations() -> tuple[str, str, str]:
    """Base URL, site URL and `owner/repo`, derived from GITHUB_REPOSITORY."""
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip() or DEFAULT_REPOSITORY
    if "/" in repository:
        owner, _, name = repository.partition("/")
    else:
        owner, name = DEFAULT_REPOSITORY.split("/")
        repository = DEFAULT_REPOSITORY
    return f"/{name}/", f"https://{owner}.github.io/{name}/", repository


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_base, default_site, repository = default_locations()
    parser = argparse.ArgumentParser(description="Build the recipe site.")
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"root-relative prefix for links and assets (default: {default_base}, "
        "or / when --serve is used)",
    )
    parser.add_argument(
        "--site-url", default=default_site, help=f"absolute site URL (default: {default_site})"
    )
    parser.add_argument(
        "--branch", default=DEFAULT_BRANCH, help="branch for 'view the source' links"
    )
    parser.add_argument("--serve", action="store_true", help="serve the output after building")
    parser.add_argument("--port", type=int, default=8000, help="port for --serve (default: 8000)")
    args = parser.parse_args(argv)
    # Serving from the output root means the site lives at /, so unless the
    # caller asked for something specific, use that rather than /recipes/.
    if args.base_url is None:
        args.base_url = "/" if args.serve else default_base
    args.base_url = normalise_base(args.base_url)
    args.site_url = args.site_url if args.site_url.endswith("/") else args.site_url + "/"
    args.repository = repository
    return args


def serve(directory: Path, port: int) -> None:
    from http.server import HTTPServer, SimpleHTTPRequestHandler

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = HTTPServer(("127.0.0.1", port), handler)
    print(f"serving {directory} at http://127.0.0.1:{port}/  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    errors: list[str] = []
    recipes = discover(errors)

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        plural = "" if len(errors) == 1 else "s"
        print(f"{len(errors)} error{plural}; nothing written.", file=sys.stderr)
        return 1

    out = prepare_out(Path(args.out))
    site = Site(
        out=out,
        base=args.base_url,
        site_url=args.site_url,
        repository=args.repository,
        branch=args.branch,
    )
    build(site, recipes)

    plural = "" if len(recipes) == 1 else "s"
    try:
        shown = out.relative_to(Path.cwd())
    except ValueError:
        shown = out
    print(f"built {len(recipes)} recipe{plural} → {shown}/")

    if args.serve:
        serve(out, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
