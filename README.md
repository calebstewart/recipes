# Recipes

A personal collection of recipes, kept as plain Markdown.

## Layout

| Directory    | What goes in it                                  |
| ------------ | ------------------------------------------------ |
| `breakfast/` | Breakfast and brunch                             |
| `mains/`     | Main dishes                                      |
| `sides/`     | Sides, salads, vegetables                        |
| `breads/`    | Breads, doughs, pastry                           |
| `desserts/`  | Sweets                                           |
| `drinks/`    | Cocktails, coffee, anything drinkable            |
| `basics/`    | Stocks, sauces, spice blends, other building blocks |

One recipe per file, named in `kebab-case.md` (e.g. `mains/chicken-piccata.md`).

## Adding a recipe

Copy [`TEMPLATE.md`](TEMPLATE.md) into the right directory and fill it in:

```sh
cp TEMPLATE.md mains/chicken-piccata.md
```

Each recipe starts with YAML frontmatter (`title`, `servings`, `prep_time`,
`cook_time`, `source`, `tags`, and an optional `nutrition` block). The
[site](#site) is generated from those fields, so keep the names consistent; add
new ones only when they'd apply to more than one recipe.

`nutrition` is a nested block of per-serving values, written as bare numbers —
grams for the macros, milligrams for sodium and cholesterol, and free text for
`serving_size`. Omit any line you don't have, or the whole block:

```yaml
nutrition:
  serving_size: 1/4 of the recipe
  calories: 420
  protein: 28
  sodium: 640
```

## Site

The collection is published at <https://calebstewart.github.io/recipes/> and
rebuilt automatically on every push to `main`. Each recipe gets its own URL:

```
https://calebstewart.github.io/recipes/mains/harissa-baked-feta-pasta/
```

Those pages embed schema.org Recipe JSON-LD, with microdata as a fallback, which
is the reason the site exists: paste a recipe URL into a recipe app's URL
importer — Umami (umami.recipes), for one — and it pulls in the title,
ingredients, and steps without any retyping.

To preview locally:

```sh
nix develop
recipes-serve   # builds with base URL / and serves on :8000
```

`nix run .#serve` does the same thing in one command. Without Nix, run the
generator directly:

```sh
python3 build.py --base-url / --out dist
```

That generator is [`build.py`](build.py), Python standard library only, so there
is nothing to install. Templates and assets live in `site/`.

A malformed recipe fails the build, so CI catches it on the pull request instead
of publishing it.

## Conventions

- Volume/weight measurements in US units, with grams alongside for baking where
  precision matters.
- Nutrition figures are estimates worked out from the ingredient list, not label
  data. Note the assumptions that move them (salt type, how much marinade or
  brine is actually eaten) in `## Notes` when they matter.
- Note the source when a recipe is adapted from somewhere else.
- Record what actually worked under `## Notes` — a recipe you've cooked twice is
  worth more than one you copied.
