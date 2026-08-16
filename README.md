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
generator through [uv](https://docs.astral.sh/uv/), which needs nothing
installed beforehand:

```sh
uv run build.py --base-url / --out dist
```

That generator is [`build.py`](build.py). It declares its dependencies in a
PEP 723 header at the top of the file, so `uv run` resolves them and picks the
interpreter; CI does exactly the same. The Nix dev shell installs those packages
into its Python, so a plain `python3 build.py` works there too. Templates and
assets live in `site/`.

A malformed recipe fails the build, so CI catches it on the pull request instead
of publishing it.

## Installing it on a phone

The site is a progressive web app, so Chrome on Android offers "Install app" and
it lands on the home screen without a browser frame. `build.py` writes the
manifest and a service worker that precaches every page, so an installed copy
opens instantly and works with no signal — which is the point, given where you
read a recipe.

The service worker's version is a hash of every built file, so each deploy
replaces the cache rather than pinning an installed copy to stale pages.

### Cooking mode

Every recipe page has a **Start cooking** button under the times. It turns the
page into something usable with your hands full:

- Ingredients get checkboxes. They are hidden and disabled the rest of the time,
  so an ordinary read of the recipe is just the list.
- **Batch** sets a multiplier — ½× to 3×, or **Other…** for a number you type,
  like 1.25 for a jar you want filled — and every amount is recalculated in
  place, the serving count with it. `render_amount()` in [`build.py`](build.py)
  finds the quantity an ingredient line leads with and hands the numbers to the
  page; a number further in is a package size (`1 (8-oz.) block feta`) or a
  per-serving aside, and neither scales with the batch. Results are rounded to
  fractions a measuring spoon can hit, and a scaled number is coloured so it
  cannot be mistaken for the written one. Times are not scaled — a double batch
  in one pan takes longer, and only the cook can see the pan — and neither is
  the nutrition table, which is per serving either way.
- Steps that name a duration get a timer button. Durations are found in the step
  prose at build time — `find_step_timers()` in [`build.py`](build.py) — so
  "Bake 20–25 min, flipping once at 10–12 min" offers two timers. A range starts
  at the **low** end, because a timer is a prompt to go look at the food.
- Running timers count down in a small card that stays out of the recipe's way.
  Several can run at once. When one finishes it posts a notification, beeps, and
  vibrates.
- The screen is asked to stay awake while cooking mode is on.
- Cooking mode, checked ingredients, the multiplier, and running timers survive a
  reload; timers are stored as absolute end times, so they resume mid-flight
  rather than restarting.

Worth knowing before you rely on it: a web page cannot ring like an alarm clock.
If the browser evicts the page, nothing fires until you open it again — at which
point an expired timer alarms immediately. The wake lock makes that unlikely
while you are actually cooking. Vibration is Android-only, and on iOS
notifications need the site installed to the home screen (16.4+).

The home screen icons are PNGs in `site/static/`, rasterized from the sources in
`site/icons/`. After editing a source, regenerate them:

```sh
nix shell nixpkgs#resvg --command sh -c '
  resvg --width 192 --height 192 site/icons/icon-source.svg site/static/icon-192.png
  resvg --width 512 --height 512 site/icons/icon-source.svg site/static/icon-512.png
  resvg --width 512 --height 512 site/icons/icon-maskable-source.svg site/static/icon-maskable-512.png
'
```

## Conventions

- Volume/weight measurements in US units, with grams alongside for baking where
  precision matters.
- Nutrition figures are estimates worked out from the ingredient list, not label
  data. Note the assumptions that move them (salt type, how much marinade or
  brine is actually eaten) in `## Notes` when they matter.
- Note the source when a recipe is adapted from somewhere else.
- Record what actually worked under `## Notes` — a recipe you've cooked twice is
  worth more than one you copied.
