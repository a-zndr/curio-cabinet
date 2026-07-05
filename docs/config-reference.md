# Config reference

A collection is defined by one `collection.yaml`. It is validated at startup;
a mistake fails loudly with a message rather than misbehaving later.

Run `curio-cabinet config-reference` to print the field types and their
per-type view defaults from the running version.

## Top level

```yaml
collection:
  title: "My Collection"     # shown in the header and titles
  slug: items                # table name + identifier; [a-z][a-z0-9_]*
  id:
    strategy: sequential     # only value supported today
    width: 4                 # zero-padded id width -> "0001"
                             # ids use [A-Za-z0-9_-] only (share URLs rely on it);
                             # CSV imports enforce this on explicit id columns
  title_field: name          # which field is the headline on cards/detail
  default_sort: {field: name, order: asc}
  accent_hue: 45             # optional: OKLCH hue for the UI accent (0-360)
  monogram: z                # optional: favicon letter(s), 1-2 chars;
                             # defaults to the title's first letter
  must_have_photos: false    # photoless items appear on the admin
                             # dashboard's "To finish" list

fields:  [ ... ]             # see below
groups:  [ ... ]             # detail-page + admin-form sections
presets: [ ... ]             # optional "specialty tables" (see below)
```

## Fields

Each entry under `fields:`:

```yaml
- key: length              # snake_case; becomes the column name
  label: Length            # human label (also a CSV header)
  type: number             # see types below
  required: false          # empty value rejected on write if true
  must_have: false         # soft-required: never blocks a save, but items
                           # missing it show on the admin dashboard's
                           # "To finish" list (see also
                           # collection.must_have_photos for photos)
  private: false           # admin-only: shown to you on detail/admin pages,
                           # never on the public table/cards/filters/search/
                           # analytics/share pages. Private fields can't be
                           # searchable or appear in public views.
  every_days: 60           # date fields only: maintenance cadence. Items
                           # whose date is blank or older than this many
                           # days appear on the "To finish" list
                           # (e.g. last_driven: drive every car every 60d)
  computed: "weight / (length / 100)"
                           # number/integer only: derive this field from other
                           # numeric fields with + - * / and parentheses. It
                           # becomes read-only and recalculates on every save;
                           # a missing operand or /0 leaves it blank. Run
                           # `curio-cabinet recompute` once to backfill
                           # existing rows after adding it.
  default: null            # value used when input is empty (must be valid)
  searchable: false        # text/longtext/tags: included in the ?q= search
  link: some_url_field     # text fields: render as a link to this url field
  values: [A, B, C]        # enum only: allowed values
  strict: false            # enum only: reject values outside `values`
  unit: { ... }            # number/integer only: see units
  rename_from: old_key     # migration hint (see migrations.md)
  views:
    table: false           # show as a default table column (default: off)
    card: hidden           # primary | secondary | hidden
    detail: true           # show on the detail page
    filter: none           # none | multi | range
    sort: true             # allow sorting by this field
    pivot: [group]         # any of: group, avg, min, max, sum
```

Every `views` key is optional and falls back to a per-type default. `table`
defaults to **off** so adding a field never silently changes the public table.

### Types

| type       | stored as        | notes |
|------------|------------------|-------|
| `text`     | TEXT             | single line |
| `longtext` | TEXT             | multi-line; excluded from tables by default |
| `number`   | REAL             | supports `unit` |
| `integer`  | INTEGER          | supports `unit`; whole numbers only |
| `boolean`  | INTEGER (0/1)    | "Yes"/"No" in the UI |
| `enum`     | TEXT             | needs `values`; `strict` controls new values |
| `tags`     | TEXT (JSON array)| comma-separated input; multi-select filter |
| `url`      | TEXT             | must start with http:// or https:// |
| `date`     | TEXT (ISO-8601)  | `YYYY-MM-DD` |

### Units

A `number`/`integer` field can carry a unit. Two forms:

```yaml
# convertible: one canonical stored unit, one or more display units
unit: {dimension: length, store: cm, display: [cm, in]}

# label only: a suffix with no conversion
unit: {label: "g/m"}
```

Known dimensions: `length` (mm, cm, m, in, ft) and `mass` (g, kg, oz, lb).
Input accepts a bare number (assumed to be the store unit) or a unit suffix —
`"24 in"`, `"6.5 ft"`, `"198 cm"` all store correctly. An unrecognized suffix
is a hard error, never a silent guess. The detail page shows every display
unit; tables and cards show the first. Range filters are entered in the first
display unit and converted for you.

## Groups

Groups define the sections on the detail page and admin form, and can be shown
conditionally.

```yaml
groups:
  - key: core
    label: Overview
    fields: [brand, type, description]
  - key: saw
    label: Saw Details
    when: {field: type, eq: Saw}    # only shown when type == "Saw"
    fields: [tpi, tooth_pattern]
```

- Every field must belong to exactly one group. Fields you don't place are
  auto-collected into an implicit "Other" group so nothing is ever invisible.
- `when` supports `eq` (equals) or `in` (one of a list). It controls only
  detail/form visibility — a conditional field still participates in the
  table, filters, and pivot like any other column.

## Presets (specialty tables)

A preset is a named, type-scoped table view: a row filter plus a curated
column set, shown as a tab above the table (All · Planes · Chisels · Saws · …).
Selecting one navigates to `?view=table&preset=<key>` — a shareable URL.

```yaml
presets:
  - key: saws                        # snake_case; used in the URL
    label: Saws                      # tab label
    filter: {field: type, in: [Saw, Backsaw, Coping Saw]}   # eq or in
    columns: [maker, type, length, tpi, condition]
```

- `filter` uses the same `eq`/`in` form as a group's `when`, and scopes the
  rows (it pre-selects those values in the filter panel too).
- `columns` lists the fields to show, in order. Any field is allowed; the
  in-browser column picker still works on top and keeps the preset scope.
- Presets are view-only — they never touch the schema, so adding or editing
  one takes effect immediately with no migration.

## Rethemeing

Set `collection.accent_hue` for a one-line rebrand, or ship an instance
stylesheet overriding any `--*` custom property from `static/css/tokens.css`.
