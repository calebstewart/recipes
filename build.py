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
#: Where DEFAULT_REPOSITORY is published. The custom domain is set on the
#: owner's user site (calebstewart.github.io), not in this repo, so it can't be
#: derived from GITHUB_REPOSITORY; forks fall back to their github.io URL.
DEFAULT_SITE_URL = "https://calebstew.art/recipes/"

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
    "yield",
    "min_batch",
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
    #: The Markdown the item was rendered from. The shopping list reads links
    #: and `{a | b}` choices out of it, which the two rendered forms have lost.
    raw: str = ""


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
    #: How much one batch makes (`1 1/2 c.`), so a recipe that calls for
    #: `3 Tbsp.` of this one can be costed in batches on the shopping list.
    yield_text: str | None = None
    #: The least the plan page will make of this as a sub-recipe, in batches.
    #: For things worth a whole batch even when a recipe needs a spoonful, like
    #: a paste that keeps for a month.
    min_batch: float | None = None

    @property
    def id(self) -> str:
        return f"{self.category}/{self.slug}"

    @property
    def servings_display(self) -> str | None:
        """`4` means four servings; `12 cookies` already says what it means."""
        if not self.servings:
            return None
        if re.fullmatch(r"[\d\s./-]+", self.servings):
            count = self.servings.strip()
            return f"{count} serving" if count == "1" else f"{count} servings"
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

    return Item(html=html_form.strip(), text=collapse(text_form), raw=raw)


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
            try:
                expand_choices(section)
            except ValidationError as exc:
                fail(str(exc))
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

    min_batch: float | None = None
    raw_min = as_text(meta.get("min_batch"))
    if raw_min:
        # A number or a written fraction: `1`, `0.5`, `1/2`.
        if re.fullmatch(r"\d+(?:\.\d+)?|\d+/\d+|\d+\s+\d+/\d+", raw_min):
            min_batch = amount_value(raw_min)
        if not min_batch:
            fail(f"min_batch must be a positive number of batches, not {raw_min!r}")
            min_batch = None

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
        yield_text=as_text(meta.get("yield")) or None,
        min_batch=min_batch,
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
# Shopping list
# --------------------------------------------------------------------------
#
# The plan page (`/plan/`) turns "these recipes, this many meals each" into a
# shopping list. Everything that needs judgement happens here, at build time,
# so a recipe the list cannot make sense of fails CI instead of quietly
# producing a wrong list. The browser only multiplies and adds.
#
# Three conventions in the Markdown feed it, none of which change how a recipe
# reads on its page or in a recipe app's importer:
#
# - A link to another recipe in an ingredient line makes that recipe a
#   sub-recipe: `2 Tbsp. [harissa paste](…/basics/harissa-paste/)`. The amount
#   is costed in batches against the sub-recipe's `yield:`, or written as one
#   directly (`1/5 of a batch of [Cava-style chicken](…)`).
# - `(optional)` at the end of a line, or of a `###` group heading, makes the
#   line something the plan can leave out.
# - `{brown rice | quinoa}` is a choice between alternatives. The page shows
#   "brown rice or quinoa"; the plan asks which, and remembers.

CATALOG_PATH = REPO_ROOT / "ingredients.yml"

#: The catalog section whose items are things you probably have, listed apart
#: so they are checked at home rather than bought by reflex.
PANTRY_SECTION = "pantry"
#: The catalog section for things a recipe uses but nobody buys: water, ice.
NONE_SECTION = "none"
OTHER_SECTION = ("other", "Other")

_OPTIONAL_RE = re.compile(r"\s*\(optional\)\s*$", re.IGNORECASE)
_CHOICE_RE = re.compile(r"\{([^{}]*)\}")
_PAREN_RE = re.compile(r"\s*\([^()]*\)")
_UNIT_WORD_RE = re.compile(r"([A-Za-z]+)\.?(?=[\s(,]|$)")
_BATCH_OF_RE = re.compile(r"^of\s+an?\s+batch\s+of\s+", re.IGNORECASE)
#: How a measure is filled, written between the amount and the unit:
#: `1 packed c. mint`. Stepped over so the unit is still found.
_FILL_RE = re.compile(
    r"^(?:(?:loosely|firmly|lightly|tightly)\s+)?"
    r"(?:packed|heaping|heaped|level|scant|generous|rounded)\s+",
    re.IGNORECASE,
)
#: Descriptions written ahead of the name with a comma after them —
#: `boneless, skinless chicken thighs` — which are not where the prep starts.
_LEADING_DESCRIPTORS = {"boneless", "skinless", "bone-in", "skin-on", "peeled", "deveined"}

#: Volume in teaspoons and weight in grams, so either can be summed across
#: recipes that wrote them differently. The value is (factor, display unit).
_VOLUME_UNITS: dict[str, tuple[float, str]] = {
    "tsp": (1, "tsp."),
    "teaspoon": (1, "tsp."),
    "tbsp": (3, "Tbsp."),
    "tablespoon": (3, "Tbsp."),
    "c": (48, "c."),
    "cup": (48, "c."),
    "pt": (96, "pt."),
    "pint": (96, "pt."),
    "qt": (192, "qt."),
    "quart": (192, "qt."),
    "ml": (0.202884, "ml"),
    "l": (202.884, "l"),
}
_WEIGHT_UNITS: dict[str, tuple[float, str]] = {
    "g": (1, "g"),
    "gram": (1, "g"),
    "kg": (1000, "kg"),
    "oz": (28.349523125, "oz."),
    "ounce": (28.349523125, "oz."),
    "lb": (453.59237, "lb."),
    "pound": (453.59237, "lb."),
}
#: Things counted rather than measured, as (singular, plural). Two of these
#: only add up when the unit — and the package size, if one is given — match.
_COUNT_UNITS: dict[str, tuple[str, str]] = {
    "bag": ("bag", "bags"),
    "batch": ("batch", "batches"),
    "block": ("block", "blocks"),
    "bottle": ("bottle", "bottles"),
    "box": ("box", "boxes"),
    "bunch": ("bunch", "bunches"),
    "can": ("can", "cans"),
    "clove": ("clove", "cloves"),
    "container": ("container", "containers"),
    "dozen": ("dozen", "dozen"),
    "head": ("head", "heads"),
    "jar": ("jar", "jars"),
    "pack": ("pack", "packs"),
    "package": ("package", "packages"),
    "packet": ("packet", "packets"),
    "pinch": ("pinch", "pinches"),
    "pouch": ("pouch", "pouches"),
    "slice": ("slice", "slices"),
    "sprig": ("sprig", "sprigs"),
    "stalk": ("stalk", "stalks"),
    "tub": ("tub", "tubs"),
}


@dataclass
class Quantity:
    """An amount and what it is measured in, ready to be summed in the browser.

    ``family`` says what can be added to what: ``volume`` (base is teaspoons),
    ``weight`` (grams), ``count:<unit>`` for counted things, and an empty
    string when there is no amount at all.
    """

    amount: float | None
    family: str
    base: float | None
    unit: str  # as displayed for one: `Tbsp.`, `8 oz. block`, ``
    plural: str  # as displayed for several: `Tbsp.`, `8 oz. blocks`, ``


@dataclass
class Choice:
    """One way of making an ingredient line, after any `{a | b}` is resolved."""

    label: str  # the alternative chosen, or "" for a line with no choice
    raw: str  # Markdown with the choice substituted in
    text: str  # plain text of the same
    quantity: Quantity
    name: str  # what is being bought: `garlic clove`, `chickpeas`
    item: str | None = None  # catalog key, for a bought ingredient
    recipe: str | None = None  # recipe id, for a sub-recipe
    batches: float | None = None  # this line's share of one sub-recipe batch


@dataclass
class ShoppingLine:
    id: str
    text: str
    group: str | None
    optional: bool
    choices: list[Choice]


@dataclass
class CatalogEntry:
    name: str
    section: str
    unit: str | None  # what a bare count of this means: `3 garlic` is 3 cloves
    buy: Purchase | None = None


@dataclass
class Purchase:
    """How an ingredient is bought, from the catalog's `buy:` lines.

    `1 head = 10 cloves` reads as: buy it by the head, and one head covers 10
    cloves. ``per`` maps each family a recipe can measure it in to how much of
    that family one purchase unit covers, in the family's base unit.

    Two kinds. A ``count`` is bought in whole things (heads, lemons, a 5 oz.
    box) and rounds up to the next one. A ``measure`` is bought by volume or
    weight with a note (`1 c. dry = 3 c.`: cooked rice bought as dry rice), and
    rounds the way any measure on the list does.
    """

    kind: str  # "count" | "measure"
    family: str  # the purchase unit's own family; recipes using it need no conversion
    one: str  # `head`, `(5 oz.) box`, `lemon`; for a measure, its unit (`c.`)
    many: str
    suffix: str  # for a measure: what it is a measure of (`dry`)
    bare: bool  # the unit is the item itself: show `5`, not `5 lemons`
    per: dict[str, float]  # recipe family → base units covered by one purchase unit


@dataclass
class Catalog:
    sections: list[tuple[str, str]]
    entries: dict[str, CatalogEntry]  # canonical name → entry
    index: dict[str, str]  # normalised name or alias → canonical name


def join_alternatives(options: list[str]) -> str:
    if len(options) == 2:
        return f"{options[0]} or {options[1]}"
    return ", ".join(options[:-1]) + f", or {options[-1]}"


def split_choices(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """Split `{a | b}` into the line as the page shows it and each way to make it.

    Returns the display Markdown (`a or b`) and a list of (label, Markdown with
    that alternative substituted). A line with no choice is its own only option,
    with an empty label.
    """
    matches = list(_CHOICE_RE.finditer(raw))
    if not matches:
        if "{" in raw or "}" in raw:
            raise ValidationError(f"unbalanced {{ }} in ingredient {raw!r}")
        return raw, [("", raw)]
    if len(matches) > 1:
        raise ValidationError(f"only one {{a | b}} choice per ingredient line: {raw!r}")
    match = matches[0]
    options = [collapse(option) for option in match.group(1).split("|")]
    if len(options) < 2 or not all(options):
        raise ValidationError(
            f"a {{ }} choice needs two or more alternatives split by |: {raw!r}"
        )
    head, tail = raw[: match.start()], raw[match.end() :]
    display = head + join_alternatives(options) + tail
    return display, [(option, head + option + tail) for option in options]


def expand_choices(section: Section) -> None:
    """Re-render ingredient lines so `{a | b}` reads as `a or b` everywhere.

    The page, the JSON-LD and the search index all take the rendered forms, so
    doing it here is what keeps the braces out of all three. ``raw`` stays the
    original, braces included, for the shopping list to read.
    """
    for group in section.groups:
        for index, item in enumerate(group.items):
            display, choices = split_choices(item.raw)
            if len(choices) > 1:
                rendered = render_inline(display)
                rendered.raw = item.raw
                match = _CHOICE_RE.search(item.raw)
                if match and not item.raw[: match.start()].strip():
                    rendered.html = _leading_choice_html(match, item.raw[match.end() :])
                group.items[index] = rendered


def _leading_choice_html(match: re.Match[str], tail: str) -> str:
    """Display HTML for a line that opens on a choice with its own amounts.

    `{1/5 of a batch of chicken | 1/4 of a batch of tempeh}` carries one amount
    per alternative, and cooking mode should scale both. Each alternative gets
    its own amount span here; render_ingredients then finds the line already
    starting with markup and leaves it alone.
    """
    options = [collapse(option) for option in match.group(1).split("|")]
    html_form = join_alternatives([render_amount(render_inline(option).html) for option in options])
    if tail.strip():
        space = " " if tail[:1].isspace() else ""
        html_form += space + render_inline(tail).html
    return html_form


def unit_key(word: str) -> str | None:
    """The table key for a unit as written — `Tbsp.`, `cups`, `pouches` — or None."""
    lowered = word.lower().rstrip(".")
    for candidate in (lowered, lowered[:-2] if lowered.endswith("es") else "",
                      lowered[:-1] if lowered.endswith("s") else ""):
        if candidate and (
            candidate in _VOLUME_UNITS or candidate in _WEIGHT_UNITS or candidate in _COUNT_UNITS
        ):
            return candidate
    return None


def _package_size(text: str) -> str:
    """`8-oz.` → `8 oz.`: the package size as it should read before a unit."""
    return collapse(re.sub(r"(\d)-(?=[A-Za-z])", r"\1 ", text))


def parse_quantity(text: str) -> tuple[Quantity, str]:
    """Read the amount and unit off the front of a plain-text ingredient line.

    Returns the quantity and the rest of the line. A range buys the top of it —
    `1 1/2 to 2 Tbsp.` is 2 — because a shopping list that comes up short is
    worse than one that leaves a spoonful over.
    """
    match = _AMOUNT_RE.match(text)
    if not match:
        return Quantity(None, "", None, "", ""), text

    amount = amount_value(match.group("high") or match.group("low"))
    rest = text[match.end() :].lstrip()

    size = ""
    if rest.startswith("("):
        close = rest.find(")")
        if close > 0:
            size = _package_size(rest[1:close])
            rest = rest[close + 1 :].lstrip()

    batch_of = _BATCH_OF_RE.match(rest)
    if batch_of:
        return Quantity(amount, "count:batch", amount, "batch", "batches"), rest[batch_of.end() :]

    rest = _FILL_RE.sub("", rest)
    key = None
    word = _UNIT_WORD_RE.match(rest)
    if word:
        key = unit_key(word.group(1))
        if key:
            rest = rest[word.end() :].lstrip()

    if key in _VOLUME_UNITS or key in _WEIGHT_UNITS:
        factor, shown = (_VOLUME_UNITS.get(key) or _WEIGHT_UNITS[key])
        family = "volume" if key in _VOLUME_UNITS else "weight"
        # A size after a measure is an equivalent (`1 c. (8 oz.) yogurt`), not
        # a package, so it is left for the name cleanup to drop.
        return Quantity(amount, family, amount * factor, shown, shown), rest

    if key:
        one, many = _COUNT_UNITS[key]
        if not size and rest.startswith("("):
            close = rest.find(")")
            if close > 0:
                size = _package_size(rest[1:close])
                rest = rest[close + 1 :].lstrip()
        if size:
            # Written the way the recipes write it: `2 (8 oz.) blocks`.
            one, many = f"({size}) {one}", f"({size}) {many}"
        return Quantity(amount, f"count:{one}", amount, one, many), re.sub(r"^of\s+", "", rest)

    if size:
        return Quantity(amount, f"count:{size}", amount, size, size), rest
    return Quantity(amount, "count:", amount, "", ""), rest


def ingredient_name(rest: str) -> str:
    """What is being bought, from what follows the quantity.

    Asides in parentheses go first (they can hold commas: a brand, a package
    size), then everything after the first comma, which is preparation:
    `garlic cloves, smashed` is bought as garlic cloves.
    """
    parts = _PAREN_RE.sub("", rest).split(",")
    while len(parts) > 1 and parts[0].strip().lower() in _LEADING_DESCRIPTORS:
        parts.pop(0)
    return collapse(parts[0]).strip(" .;:-–—")


def _singular(word: str) -> str:
    if len(word) <= 3 or word.endswith(("ss", "us", "is")):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "xes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def name_key(name: str) -> str:
    """Normalise an ingredient name for matching: case, punctuation, plurals."""
    lowered = re.sub(r"[^\w\s'-]", " ", name.lower())
    words = lowered.split()
    if not words:
        return ""
    words[-1] = _singular(words[-1])
    return " ".join(words)


def load_catalog(errors: list[str]) -> Catalog:
    """Read `ingredients.yml`: store sections, and names to merge on the list."""
    rel = CATALOG_PATH.name
    sections: list[tuple[str, str]] = []
    entries: dict[str, CatalogEntry] = {}
    index: dict[str, str] = {}
    if not CATALOG_PATH.is_file():
        warn(f"no {rel}; the shopping list will not merge or sort ingredients")
        return Catalog([OTHER_SECTION], entries, index)

    try:
        data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        errors.append(f"{rel}: not valid YAML: {exc}")
        return Catalog([OTHER_SECTION], entries, index)
    if not isinstance(data, dict):
        errors.append(f"{rel}: expected `sections:` and `ingredients:` at the top level")
        return Catalog([OTHER_SECTION], entries, index)

    raw_sections = data.get("sections") or {}
    if not isinstance(raw_sections, dict):
        errors.append(f"{rel}: `sections:` must map an id to a label")
        raw_sections = {}
    for section_id, label in raw_sections.items():
        sections.append((str(section_id), as_text(label) or str(section_id)))
    known_sections = {section_id for section_id, _ in sections} | {NONE_SECTION}
    if OTHER_SECTION[0] not in known_sections:
        sections.append(OTHER_SECTION)

    raw_entries = data.get("ingredients") or {}
    if not isinstance(raw_entries, dict):
        errors.append(f"{rel}: `ingredients:` must map a name to its details")
        raw_entries = {}

    def claim(key: str, name: str) -> None:
        if not key:
            return
        if key in index and index[key] != name:
            errors.append(f"{rel}: {key!r} is listed under both {index[key]!r} and {name!r}")
        index[key] = name

    for raw_name, details in raw_entries.items():
        name = collapse(str(raw_name))
        if isinstance(details, str):
            details = {"section": details}
        if not isinstance(details, dict):
            errors.append(f"{rel}: {name!r} must be a section name or a mapping")
            continue
        unknown = set(details) - {"section", "also", "unit", "buy"}
        if unknown:
            errors.append(f"{rel}: {name!r} has unknown keys {sorted(unknown)}")
        section = as_text(details.get("section")) or OTHER_SECTION[0]
        if section not in known_sections and section != OTHER_SECTION[0]:
            errors.append(f"{rel}: {name!r} is in section {section!r}, which `sections:` lacks")
        unit = as_text(details.get("unit")) or None
        if unit and unit_key(unit) not in _COUNT_UNITS:
            errors.append(f"{rel}: {name!r} has unit {unit!r}, which is not a counted unit")
        entries[name] = CatalogEntry(name=name, section=section, unit=unit)
        if details.get("buy") is not None:
            try:
                entries[name].buy = parse_purchase(name, details["buy"])
            except ValidationError as exc:
                errors.append(f"{rel}: {name!r}: {exc}")
        claim(name_key(name), name)
        for alias in as_list(details.get("also")):
            claim(name_key(alias), name)

    return Catalog(sections, entries, index)


def _plural_phrase(phrase: str) -> str:
    """`lemon` → `lemons`, `large onion` → `large onions`, `squash` → `squashes`."""
    head, _, word = phrase.rpartition(" ")
    lowered = word.lower()
    if lowered in ("dozen",) or not word:
        plural = word
    elif lowered.endswith(("s", "sh", "ch", "x", "o")):
        plural = word + "es"
    elif lowered.endswith("y") and lowered[-2:-1] not in ("a", "e", "i", "o", "u"):
        plural = word[:-1] + "ies"
    else:
        plural = word + "s"
    return f"{head} {plural}" if head else plural


def parse_purchase(name: str, value: object) -> Purchase:
    """Read an entry's `buy:` — one `A = B` line, or a list of them.

    Every line must buy the same thing (the left side); each right side is one
    way recipes measure the ingredient, and says how much one of the left
    covers. Two lines converting the same kind of measure would contradict
    each other, so that is an error too.
    """
    if isinstance(value, str):
        lines = [value]
    elif isinstance(value, list) and all(isinstance(line, str) for line in value):
        lines = value
    else:
        raise ValidationError("`buy:` must be a line like `1 head = 10 cloves`, or a list of them")

    purchase: Purchase | None = None
    for line in lines:
        left_raw, sep, right_raw = line.partition("=")
        if not sep or "=" in right_raw:
            raise ValidationError(f"`buy: {line}` needs exactly one `=`")
        left, left_rest = parse_quantity(collapse(left_raw))
        right, right_rest = parse_quantity(collapse(right_raw))
        if not left.amount or not right.amount or right.base is None:
            raise ValidationError(f"`buy: {line}` needs an amount on both sides")
        if right_rest.strip():
            raise ValidationError(
                f"`buy: {line}`: the right side is only an amount and a unit, "
                f"not {right_rest.strip()!r}"
            )

        left_rest = collapse(left_rest)
        if left.family in ("volume", "weight"):
            current = Purchase(
                kind="measure", family=left.family, one=left.unit, many=left.plural,
                suffix=left_rest, bare=False, per={},
            )
            covers = right.base / (left.base or 1)
        else:
            if left.family == "count:" and not left_rest:
                raise ValidationError(f"`buy: {line}`: say what is bought, e.g. `1 lemon`")
            if left.unit:
                # A unit word: `1 head`, `1 (5 oz.) box`, `1 dozen`.
                one, many = left.unit, left.plural
            else:
                # The thing itself: `1 lemon`, `1 large onion`.
                one, many = left_rest, _plural_phrase(left_rest)
            current = Purchase(
                kind="count", family=left.family, one=one, many=many, suffix="",
                bare=name_key(one) == name_key(name), per={},
            )
            covers = right.base / left.amount

        if purchase is None:
            purchase = current
        elif (purchase.kind, purchase.family, purchase.one, purchase.suffix) != (
            current.kind, current.family, current.one, current.suffix
        ):
            raise ValidationError(
                f"every `buy:` line must buy the same thing; {line!r} buys "
                f"{current.one!r} where an earlier line buys {purchase.one!r}"
            )
        if right.family in purchase.per:
            raise ValidationError(f"two `buy:` lines convert {right.family.removeprefix('count:') or 'a bare count'}")
        purchase.per[right.family] = covers

    assert purchase is not None
    return purchase


def recipe_link(raw: str, ids: set[str]) -> tuple[str | None, str | None]:
    """The recipe a Markdown line links to, as (id, error).

    Any link whose path ends in `<category>/<slug>/` is taken as a link to a
    recipe — absolute, root-relative, or relative — and must name one that
    exists. Links anywhere else (a product page, a source) are not sub-recipes.
    """
    found: list[str] = []
    for match in _LINK_RE.finditer(raw):
        path = urlparse(match.group(2).strip()).path
        parts = [part for part in path.split("/") if part and part not in (".", "..")]
        if len(parts) < 2 or parts[-2] not in CATEGORY_LABELS:
            continue
        recipe_id = f"{parts[-2]}/{parts[-1].removesuffix('.md')}"
        if recipe_id not in ids:
            return None, f"links to {recipe_id!r}, which is not a recipe here"
        found.append(recipe_id)
    if len(set(found)) > 1:
        return None, "links to more than one recipe; a sub-recipe line needs exactly one"
    return (found[0] if found else None), None


def yield_quantity(recipe: Recipe) -> Quantity | None:
    if not recipe.yield_text:
        return None
    quantity, _rest = parse_quantity(recipe.yield_text)
    return quantity if quantity.amount else None


def servings_count(recipe: Recipe) -> float:
    """How many meals one batch makes. A recipe that does not say is one meal."""
    if recipe.servings:
        match = _AMOUNT_RE.match(recipe.servings)
        if match:
            value = amount_value(match.group("low"))
            if value > 0:
                return value
    return 1.0


def shopping_lines(
    recipe: Recipe,
    by_id: dict[str, Recipe],
    catalog: Catalog,
    errors: list[str],
    uncatalogued: dict[str, str],
) -> list[ShoppingLine]:
    if recipe.ingredients is None:
        return []

    def fail(message: str) -> None:
        errors.append(f"{recipe.rel_path}: {message}")

    lines: list[ShoppingLine] = []
    seen_ids: set[str] = set()
    for group in recipe.ingredients.groups:
        group_text = group.name.text if group.name is not None else None
        group_optional = bool(group_text and _OPTIONAL_RE.search(group_text))
        for item in group.items:
            source = item.raw or item.text
            optional = group_optional or bool(_OPTIONAL_RE.search(source))
            source = _OPTIONAL_RE.sub("", source)
            try:
                _display, options = split_choices(source)
            except ValidationError:
                continue  # load_recipe has already reported it

            choices: list[Choice] = []
            for label, raw in options:
                text = render_inline(raw).text
                quantity, rest = parse_quantity(text)
                name = ingredient_name(rest)
                choice = Choice(
                    label=choice_label(label),
                    raw=raw,
                    text=text,
                    quantity=quantity,
                    name=name,
                )
                linked, problem = recipe_link(raw, set(by_id))
                if problem:
                    fail(f"ingredient {text!r} {problem}")
                    continue
                if linked:
                    if linked == recipe.id:
                        fail(f"ingredient {text!r} links to its own recipe")
                        continue
                    choice.recipe = linked
                    choice.batches = sub_recipe_batches(choice, by_id[linked], fail)
                else:
                    key = name_key(name) or name_key(text)
                    canonical = catalog.index.get(key)
                    if canonical is None:
                        uncatalogued.setdefault(key, recipe.rel_path)
                        choice.item = key
                    else:
                        choice.item = canonical
                        unit = catalog.entries[canonical].unit
                        if unit and quantity.family == "count:":
                            one, many = _COUNT_UNITS[unit_key(unit) or unit]
                            quantity.family = f"count:{one}"
                            quantity.unit, quantity.plural = one, many
                choices.append(choice)

            if not choices:
                continue
            base_id = slugify(" ".join(filter(None, [group_text, choices[0].name or choices[0].text])))
            line_id, n = base_id, 2
            while line_id in seen_ids:
                line_id, n = f"{base_id}-{n}", n + 1
            seen_ids.add(line_id)
            lines.append(
                ShoppingLine(
                    id=line_id,
                    text=item.text,
                    group=group_text,
                    optional=optional,
                    choices=choices,
                )
            )
    return lines


def choice_label(raw: str) -> str:
    """The button text for one alternative: what it is, without its amount.

    `1/5 of a batch of [Cava-style chicken](…)` is offered as "Cava-style
    chicken"; the amount is in the line's own text beside the buttons.
    """
    if not raw:
        return ""
    text = render_inline(raw).text
    quantity, rest = parse_quantity(text)
    return collapse(rest) if quantity.amount is not None and rest.strip() else text


def sub_recipe_batches(choice: Choice, child: Recipe, fail) -> float | None:
    """How much of one batch of ``child`` this line uses, at the parent's 1×."""
    quantity = choice.quantity
    if quantity.amount is None:
        fail(
            f"ingredient {choice.text!r} links to {child.id} but gives no amount, "
            "so the shopping list cannot tell how much of it to make"
        )
        return None
    if quantity.family == "count:batch":
        return quantity.amount
    made = yield_quantity(child)
    if made is None:
        fail(
            f"ingredient {choice.text!r} uses {child.id} by the {quantity.unit or 'count'}, "
            f"so {child.rel_path} needs a `yield:` (e.g. `yield: 1 1/2 c.`), "
            "or write the line as a fraction of a batch"
        )
        return None
    if made.family != quantity.family or not made.base:
        fail(
            f"ingredient {choice.text!r} is measured in {quantity.unit or 'a count'}, "
            f"which cannot be compared with {child.id}'s yield of {child.yield_text!r}"
        )
        return None
    return (quantity.base or 0) / made.base


def topological_order(graph: dict[str, set[str]], errors: list[str]) -> list[str]:
    """Recipes ordered so each comes before every recipe it uses.

    The plan page walks this order once, handing each recipe's needs down to
    its sub-recipes, so a sub-recipe's total is complete before it is read.
    A cycle would make that total undefined, so it fails the build.
    """
    order: list[str] = []
    state: dict[str, int] = {}  # 1 visiting, 2 done

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            loop = trail[trail.index(node) :] + [node]
            errors.append("recipes use each other in a loop: " + " → ".join(loop))
            return
        state[node] = 1
        for child in sorted(graph.get(node, ())):
            visit(child, trail + [node])
        state[node] = 2
        order.append(node)

    for node in sorted(graph):
        visit(node, [])
    order.reverse()
    return order


def _number(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def build_shopping_data(
    site_base: str, recipes: list[Recipe], catalog: Catalog, errors: list[str]
) -> dict[str, object]:
    """Everything the plan page needs, as JSON-ready data."""
    by_id = {recipe.id: recipe for recipe in recipes}
    uncatalogued: dict[str, str] = {}
    graph: dict[str, set[str]] = {}
    entries: list[dict[str, object]] = []

    for recipe in recipes:
        lines = shopping_lines(recipe, by_id, catalog, errors, uncatalogued)
        made = yield_quantity(recipe)
        graph[recipe.id] = {
            choice.recipe for line in lines for choice in line.choices if choice.recipe
        }
        entries.append(
            {
                "id": recipe.id,
                "title": recipe.title,
                "url": f"{site_base}{recipe.category}/{recipe.slug}/",
                "category": recipe.category,
                "categoryLabel": recipe.category_label,
                "servings": _number(servings_count(recipe)),
                "servingsLabel": recipe.servings_display or "",
                "yield": recipe.yield_text or "",
                "yieldFamily": made.family if made else "",
                "yieldBase": _number(made.base) if made else None,
                "minBatch": _number(recipe.min_batch),
                "lines": [
                    {
                        "id": line.id,
                        "text": line.text,
                        "group": line.group,
                        "optional": line.optional,
                        "choices": [
                            {
                                "label": choice.label,
                                "text": choice.text,
                                "name": choice.name,
                                "item": choice.item,
                                "recipe": choice.recipe,
                                "batches": _number(choice.batches),
                                "amount": _number(choice.quantity.amount),
                                "family": choice.quantity.family,
                                "base": _number(choice.quantity.base),
                                "unit": choice.quantity.unit,
                                "plural": choice.quantity.plural,
                            }
                            for choice in line.choices
                        ],
                    }
                    for line in lines
                ],
            }
        )

    for key, rel_path in sorted(uncatalogued.items()):
        warn(f"{CATALOG_PATH.name} has no entry for {key!r} (first seen in {rel_path}); "
             "it will be listed under Other")

    measured: dict[str, set[str]] = {}
    for entry in entries:
        for line in entry["lines"]:
            for choice in line["choices"]:
                if choice["item"] and choice["amount"] is not None:
                    measured.setdefault(choice["item"], set()).add(choice["family"])

    items: dict[str, dict[str, object]] = {}
    for name, entry in catalog.entries.items():
        items[name] = {"name": entry.name, "section": entry.section}
        buy = entry.buy
        if buy is None:
            continue
        for family in sorted(set(buy.per) - measured.get(name, set())):
            shown = family.removeprefix("count:") or "a bare count"
            warn(f"{CATALOG_PATH.name}: {name!r} converts {shown}, which no recipe measures it in")
        items[name]["buy"] = {
            "kind": buy.kind,
            "family": buy.family,
            "one": buy.one,
            "many": buy.many,
            "suffix": buy.suffix,
            "bare": buy.bare,
            "per": {family: _number(covers) for family, covers in buy.per.items()},
        }
    for key in uncatalogued:
        items[key] = {"name": key, "section": OTHER_SECTION[0]}

    return {
        "sections": [
            {"id": section_id, "label": label, "pantry": section_id == PANTRY_SECTION}
            for section_id, label in catalog.sections
        ],
        "items": items,
        "recipes": entries,
        "order": topological_order(graph, errors),
    }


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


#: Fractions written as one character, and what they are worth.
_VULGAR = {
    "½": 0.5,
    "⅓": 1 / 3,
    "⅔": 2 / 3,
    "¼": 0.25,
    "¾": 0.75,
    "⅕": 0.2,
    "⅙": 1 / 6,
    "⅛": 0.125,
    "⅜": 0.375,
    "⅝": 0.625,
    "⅞": 0.875,
}

#: One quantity, in the forms people write: `2`, `1.5`, `1/2`, `1 1/2`, `1½`, `½`.
_QUANTITY = (
    r"\d+\s+\d+/\d+"
    rf"|\d+\s*[{''.join(_VULGAR)}]"
    r"|\d+/\d+"
    r"|\d+(?:\.\d+)?"
    rf"|[{''.join(_VULGAR)}]"
)

#: The amount an ingredient line leads with, optionally a range (`2 to 4 Tbsp.`).
#: Anchored at the start on purpose: a number later in the line is a size on a
#: package (`1 (8-oz.) block feta`) or a per-serving aside, and neither scales
#: with the batch. A leading hedge is stepped over so `About 1 oz.` still scales.
_AMOUNT_RE = re.compile(
    r"^(?:(?:about|around|roughly|approximately)\s+)?"
    rf"(?P<low>{_QUANTITY})"
    rf"(?:(?P<sep>\s*(?:to|or|[–—-])\s*)(?P<high>{_QUANTITY}))?"
    r"(?=[\s(]|$)",
    re.IGNORECASE,
)

_PART_RE = re.compile(
    rf"(\d+)/(\d+)|(\d+(?:\.\d+)?)|([{''.join(_VULGAR)}])"
)


def amount_value(raw: str) -> float:
    """What a written quantity is worth. `1 1/2` and `1½` are both 1.5."""
    total = 0.0
    for match in _PART_RE.finditer(raw):
        if match.group(1):
            total += int(match.group(1)) / int(match.group(2))
        elif match.group(3):
            total += float(match.group(3))
        else:
            total += _VULGAR[match.group(4)]
    return total


def render_amount(html: str) -> str:
    """Wrap a leading quantity so cooking mode can scale it.

    The span carries the numbers; its text stays exactly what was written, so
    the page reads the same and JavaScript only has to swap the text back in
    when the multiplier returns to 1×.
    """
    match = _AMOUNT_RE.match(html)
    if not match:
        return html

    low = amount_value(match.group("low"))
    if low <= 0:
        return html

    attrs = f' data-amount="{low:.6g}"'
    end = match.end("low")
    if match.group("high"):
        high = amount_value(match.group("high"))
        if high > 0:
            attrs += f' data-amount-high="{high:.6g}"'
            attrs += f' data-amount-sep="{esc(match.group("sep"))}"'
            end = match.end("high")

    start = match.start("low")
    return (
        f'{html[:start]}<span class="amount"{attrs}>{html[start:end]}</span>'
        f"{html[end:]}"
    )


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
                f'<span itemprop="recipeIngredient">{render_amount(item.html)}</span>'
                "</label></li>"
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
        # Scalable too: a doubled ingredient list that still claims to serve
        # four would be lying to the cook.
        rows.append(
            (
                "Serves",
                f'<dd itemprop="recipeYield">{render_amount(esc(recipe.servings))}</dd>',
            )
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
        self.plan_tmpl = self._load(templates / "plan.html")

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

    def nav(self, *links: tuple[str, str]) -> str:
        """The header links: the plan page on every page, after any others."""
        links = links + (("Plan & shop", f"{self.base}plan/"),)
        anchors = "".join(f'<a href="{esc(href)}">{esc(label)}</a>' for label, href in links)
        return f'<nav class="site-nav">{anchors}</nav>'

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
        nav=site.nav(),
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
            "recipe_id": esc(recipe.id),
            "servings_count": f"{servings_count(recipe):g}",
            "plan_url": esc(f"{site.base}plan/"),
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
        nav=site.nav(("All recipes", site.base)),
        scripts=(
            f'<script src="{esc(site.base)}assets/recipe.js" defer></script>\n'
            f'<script src="{esc(site.base)}assets/cooking.js" defer></script>'
        ),
    )


PLAN_DESCRIPTION = "Pick the week's recipes and how many meals of each; get the shopping list."


def render_plan_page(site: Site, shopping_json: str) -> str:
    canonical = site.absolute("plan/")
    content = site.plan_tmpl.safe_substitute({"shopping_json": shopping_json})
    return site.shell(
        title=esc(f"Plan & shop — {SITE_TITLE}"),
        meta_description=esc(PLAN_DESCRIPTION),
        canonical=esc(canonical),
        body_class="plan-page",
        head_extra=social_meta(
            title=f"Plan & shop — {SITE_TITLE}",
            description=PLAN_DESCRIPTION,
            url=canonical,
            kind="website",
            image=None,
        ),
        content=content,
        nav=site.nav(("All recipes", site.base)),
        scripts=f'<script src="{esc(site.base)}assets/plan.js" defer></script>',
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

// Timer alarms and the page they came from are two views of one thing, so they
// are kept in step: the page closes the notification when its timer is
// dismissed, and these handlers tell the page when the notification is the one
// that was dismissed. Messages carry the recipe URL because timer ids are
// per-page counters — two recipes open at once would otherwise collide.
function tellPages(message) {
  return self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
    clients.forEach((client) => client.postMessage(message));
  });
}

// Cooking mode posts its alarms through this registration, because
// `new Notification()` throws on Android Chrome. Tapping one should land on the
// recipe that set it rather than opening a second copy of the site.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = data.url || START_URL;

  event.waitUntil(
    tellPages({ type: 'timer-acknowledged', id: data.timerId, url: data.url })
      .then(() => self.clients.matchAll({ type: 'window', includeUncontrolled: true }))
      .then((clients) => {
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

// Swiping the notification away is a dismissal: drop the timer with it.
self.addEventListener('notificationclose', (event) => {
  const data = event.notification.data || {};
  event.waitUntil(tellPages({ type: 'timer-dismissed', id: data.timerId, url: data.url }));
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


def build(site: Site, recipes: list[Recipe], shopping: dict[str, object]) -> None:
    out = site.out
    index_json = dump_json(build_search_index(site, recipes))

    write(out / "index.html", render_home(site, recipes, index_json))
    write(out / "plan" / "index.html", render_plan_page(site, dump_json(shopping)))
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
    if repository == DEFAULT_REPOSITORY:
        site_url = DEFAULT_SITE_URL
    else:
        site_url = f"https://{owner}.github.io/{name}/"
    return f"/{name}/", site_url, repository


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
    catalog = load_catalog(errors)
    shopping = build_shopping_data(args.base_url, recipes, catalog, errors)

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
    build(site, recipes, shopping)

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
