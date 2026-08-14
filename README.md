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
`cook_time`, `source`, `tags`) so the collection stays machine-readable if it
ever grows a static site or search. Keep the field names consistent; add new
ones only when they'd apply to more than one recipe.

## Conventions

- Volume/weight measurements in US units, with grams alongside for baking where
  precision matters.
- Note the source when a recipe is adapted from somewhere else.
- Record what actually worked under `## Notes` — a recipe you've cooked twice is
  worth more than one you copied.
