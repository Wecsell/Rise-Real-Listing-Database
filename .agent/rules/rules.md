# Workspace Rules: Rise Real Bali Listing Database

## 1. Stack & Architecture
- Python 3.11+, Docker, asyncio.
- Pipeline: Telegram Userbot -> Postgres -> Airtable.
- Main logic resides in `/app`. Always run via `docker-compose`. Never modify `init.sql` without explicit approval.
- Entry points run as modules: `python -m app.listener`. Intra-package imports **must** be `app.*`;
  a flat `from gemini_parser import ...` loads the module a second time under a different name,
  giving it its own client and its own cached state. `tests/test_wiring.py` enforces this.

## 2. Airtable Environment & Base IDs
The active base is determined dynamically by the `AIRTABLE_BASE_ID` environment variable in `.env`:
- **TEST Base ID**: `appsAbRs7DnYYWFt6` ("Base RR New Test") — *active by default; currently the only base the token can reach*.
- **PROD Base ID**: `app2IEMPr6R3GelVP` ("Base RR New") — *production deployment*.

> Schema changes made on TEST are **not** replicated to PROD automatically and must be replayed there
> before deployment. Airtable's API cannot create or delete select options — only fields can be created
> and renamed. Options are added by writing a record with `typecast: true`, and can only be **deleted**
> by hand in the interface.

### Table Mapping:
- **Projects**: `tbl15zdeaF04TLXSe` (PROD) / Name `"Projects"` (MCP)
- **Units**: `tblutK0qMdyPOjidT` (PROD) / Name `"Units"` (MCP)
- **Developer**: `tblhtsoZ8HXdU61fc` (PROD) / Name `"Developer"` (MCP)
- **Field Staging**: name `"Field Staging"` — intake for field findings.

### Business Canons:
- Key format: `project__unitno__Nbr` or `project__type__Nbr__views` (STRICTLY NO PRICES in Key!).
- Prices strictly in USD (number).
- Never modify formula fields (`Unit ID`, `Price per m²`).
- Image URL format: `https://drive.google.com/thumbnail?id={FILE_ID}&sz=w2000`.
- All column names and select values are in **English**.
- 🚨 **`Units` holds TYPOLOGY, never the individual lots.** This is the single most repeated
  mistake in this base (owner, 07.08.2026 — restated after it kept happening). A chessboard with
  32 one-bedroom apartments of the same layout is **ONE** record, not 32. Group the developer's
  lot list by typology first, write one row per typology. Availability is `On sale` / `Sold` for
  the whole type; per-lot numbers, floors and statuses do **not** belong here.
  - Two genuinely different products that collide on the same type+bedrooms (Kuara's 1BR
    "Garden Villa" 65 m² vs 1BR "River House" 123 m²) are separated by passing the product name
    in `Unit Number` — it lands in `Unit ID` and makes the `Key` unique. That is the ONLY reason
    to create a second row for the same bedroom count.
  - `Total Units` on the **project** is where the physical count goes (16 villas, 274 suites).
  - **Enforced at write time, not just cleanup.** `tools_manual_intake.validate_payload()` calls
    `airtable_client.find_typology_violations()` — a payload where several `units` entries share
    type+bedrooms+price+area and differ only by a lot-code `Unit Number` (`a4`, `alt220`, `unit 12`)
    is rejected outright with an actionable message. A payload where the differing token is a real
    product name (`Topaz` vs `Jade-Pool-1st-floor`) passes untouched — same heuristic used to
    unwind the 250-record cleanup on 07.08.2026 (see `unit-dedup-check-key-token-not-just-price`
    in memory). If this check ever fires, don't work around it — collapse the payload to one row
    per typology before retrying.
- `Units.Stage` and `Units.Area` are **lookups** from the linked project, not editable selects. Writing
  to either fails the whole record with 422. Stage follows `Projects.Construction stage` by itself.
- Complex-wide amenities belong on the unit too — a buyer of one villa gets the shared gym. "фитнес и
  йога-зона" → `Gym`; "ресторан и лаунж" → `Restaurant`; "приватный бассейн" → `Pool = Yes(Private)`.
  A villa with a garden has a terrace: `Terrace/Balcony = Yes`.
- Exact option spellings that differ from the obvious guess: `Freehold` is `yes` / **`not`** (lowercase,
  not "no"); `Terrace/Balcony` is `Yes` / **`Not`**.
- `Units.Group with agency` is a **lookup on `Source`** (owner, 06.08.2026) — the code never writes it,
  it follows whatever `Source` says. Getting `Source` right is the whole job. The origin chat is carried
  end to end: `listener` → `payload["chat_title"]` → `sync_job` → `upsert_project` / `upsert_unit` →
  `source_label()`.
- **`strip_computed_fields` must be the last step before the write**, after every assignment. When it sat
  earlier in the function, the `Group with agency` assignment below it slipped past — `field_exists` said
  the field existed, so nothing objected, and every unit write with a known chat would have 422'd.
  A guard that only covers part of the payload is not a guard.
  Two caveats. Records written before 06.08.2026 cannot be backfilled — their origin was never stored,
  and `Source` on all ~700 of them is the old constant. And `UNKNOWN_SOURCE` is literally
  `"TG: Rise Real Bali Chat"`, so `Source` alone cannot distinguish "origin not recorded" from "came from
  a chat of that name"; the string is kept because `Source` is rewritten on every upsert and changing it
  would silently overwrite those 700 records. Use `Group with agency` — set only when the chat is known —
  as the reliable signal.
- **`Source` only ever improves (`resolve_source`).** `Manual` is upgraded to a chat name once the parser
  finds the project in a listened chat, and never downgraded back — a later manual import must not erase
  the connect, which is the whole point of the field: an agent needs to see which chat gives us a line to
  this object. Different chats of equal standing accumulate (`TG: A | TG: B`, capped at 3, oldest first).
  A project normally has exactly one chat — **we** are the agency, the chats belong to developers, so
  there is nothing to duplicate. A second chat is therefore a fact in itself: sales moved to a new group,
  or the developer resold the project. Overwriting the old chat would erase that history, so the field
  reads chronologically: leftmost is where the project came from, rightmost where it is now.
  The legacy placeholder and bare URLs count as "no
  connect", so a real chat name displaces them instead of sitting beside them — which is how those ~700
  records heal themselves. Re-running on unchanged data must not change the value.
- **`Source` records how the material reached us, never where it lives.** Format `channel: chat`, and the
  channels are a closed set of three (`airtable_client.KNOWN_CHANNELS`), because there are only three
  ways in: `TG: <chat>` (the listener), `WA: <chat>` (once WhatsApp ships), `Manual` (the owner sent a
  link). Notion, a developer's site or an agent dashboard are **not** channels — the parser never reaches
  them on its own, their link always arrived by one of the three. Writing a URL into `Source` loses who
  supplied it; five records in the base are mislabelled that way. `Manual` carries no person's name.
  Within TG and WA the chat may be a group **or** a private chat — the Telegram folder holds both — so the
  label names the chat and never asserts "group".
- Chat membership is read from a **Telegram folder** (`listener.resolve_folder_chat_ids`), so adding a
  group there is enough to start collecting it — but the folder is resolved **at startup only**, so a
  newly added group is picked up on the next restart, not live. A chat title is matched to an existing
  developer by `fuzzy_match_developer` (threshold 0.4), which is what keeps a renamed or newly added
  group from creating a duplicate developer.
- `Field Staging.Priority` accepts only `Hight` / `Medium` / `Low`. `Hight` is a typo in the base,
  but it is the real option name. Russian values are rejected — normalise via
  `priority_parser.to_airtable_priority()`.
- Source hierarchy when data conflicts: availability chart (шахматка) > developer's answers in chat >
  their materials. Master record precedence: Telegram group > field finding or research.
- **The availability chart always wins — there is no exception.** For price, area and availability it is
  the only authority: a chat post advertising a different figure does not overwrite it, however recent or
  emphatic. Those posts are usually special offers on a single unit, and writing one into the type-level
  record silently misprices the whole type. A dedicated field for special offers is planned (owner,
  06.08.2026); until it exists, such a figure goes nowhere — not into the price, not "temporarily".
  Everything the chart does not cover (zoning, permits, leasehold terms, amenities) is fair game from
  chat, and there chat outranks the developer's own materials.
- A finding reaches Developer/Projects/Units **only** after the `Confirmed` checkbox is ticked.
  Field data is a hypothesis until then.
- **`Coordinates(for Map)` stores `longitude, latitude`** — for Bali the value starts with `115`
  (owner-confirmed 06.08.2026). Do not "fix" this order to please a map.
- **The Interfaces map works with `Coordinates(for Map)` exactly as stored** — Address field points
  straight at it, `longitude, latitude`, and pins land correctly (verified in the live interface
  06.08.2026: the K-Village pin sits on the east Bukit where its coordinate says). Set Label field to
  `Project Name`, or pins are captioned with raw numbers. **No derived or duplicate field is needed** —
  `Map Point` (formula) and `Map Pin` (text) were created while chasing a phantom and are redundant.
- **An empty map usually means the map is still geocoding, not that the data is wrong.** The map keeps a
  geocode cache; wiping it forces a full re-geocode of every record, and for a few minutes the map shows
  nothing or a partial set. On 06.08.2026 that transient emptiness was misread as a coordinate-order bug
  and cost an hour: seven rewrites of live coordinates, two redundant fields, and one moment where a
  working point was removed from the map. **Before touching coordinates, open the map's settings and wait
  for it to finish loading.** `Map Cash` was that cache (an old Map extension's, `blockInstallationIds`
  in its base64 payload) — clearing it is what triggered the re-geocode.
- **`Active` is the human-review gate, and code never writes it** (`airtable_client.HUMAN_ONLY_FIELDS`).
  Ticked by a person in the Airtable UI; unticked means the record has not been reviewed and is not
  shown. Visibility is a view filter on `Active` — the API cannot create views, so that filter is set
  once in the base UI. Clearing the box is as much interference as ticking it, so the guard strips the
  field in both directions, and `tools_manual_intake.py` rejects it in its input: a review that arrives
  in a JSON file is not a review.
- **`Active` also blocks rewrites, since 06.08.2026 (owner's request).** Until then the checkbox was a
  view filter with no other effect — a verified project and its units were overwritten by the next parse
  exactly like an unverified one. Now `upsert_project`, `upsert_unit`, `mark_project_units_sold` and
  `doc_pipeline.save_findings_to_gaps` all check the existing record's `Active` before writing and skip
  entirely if it is set. `upsert_unit` returns the truthy sentinel `airtable_client.SKIPPED_ACTIVE`, not
  `None`, when it skips — callers such as `field_processor.py` and `app/sync_job.py` read a falsy result
  as a write failure, and a plain `None` made an Active project look like a broken write (Field Staging
  retried forever every 30s; the sync queue burned its retries and landed in `failed`). Manual runs
  (`tools_manual_intake.py`) force a cache refresh before writing, since a checkbox ticked seconds ago in
  the UI can otherwise sit outside the ~10-minute cache TTL and the guard would silently miss it.
  `tools_merge_duplicates.py` — the one place that writes `Active` at all, clearing it on the losing
  side of a merge — now skips a pair entirely if either the keeper or the loser is Active, rather than
  merging over a human's verification.
- `Status` is **not** a review flag. It is derived entirely from `gaps` and recomputed on every run, so a
  human verdict placed there would be silently overwritten. `Verified` means only "no gaps were found" —
  a machine's claim about completeness. As of 06.08.2026 the bot had set it on 451 units and 57 projects
  that nobody had checked, which is exactly why the review flag had to live elsewhere.
- Legal risk (Land Zoning `Green`, or missing PBG) forces `Priority = Low` regardless of what the
  agent said in the voice note.

## 3. Never hardcode what the base already knows
Hardcoded copies of the schema drift and then lose data silently. This has already cost the project the
`District` field on 33 of 47 projects, plus mismatches on `Priority`, `Property Type` and
`Land Zoning Color`.

- Read select options and field names through `airtable_client.get_select_options()` / `field_exists()`.
- Writing to a field absent from the base fails the **whole record write** with 422 — it is not ignored.
- Run `python -m app.schema_check` after any schema change. It compares the live base against what the
  code actually emits and is part of the test suite.

## 4. Intake pipeline: from a link to a filled card

**Trigger.** A request to "fill the table" accompanied by a Telegram group name, a developer name, or
any set of links means the **whole** pipeline below, not just the step named out loud. Do not stop after
parsing and report missing fields — steps 3 and 4 are part of the same job.

### Step 1 — Recognise the sources
The parser classifies every link it is given (`link_fetcher`, `doc_router`):
- **agent dashboard / dev kit** (Notion, Google Drive folder, site) — the narrative source: terms,
  timeline, zoning, permits, leasehold;
- **availability chart (шахматка)** — the unit-level source: types, areas, prices, availability.

Both are followed. Where they disagree, the availability chart wins (§2, source hierarchy).

### Step 2 — Follow the links and collect
**Current mode: extraction is manual, `LLM_BACKEND=off`.** No call reaches Gemini until the table is
filled correctly and the filling rules are settled — a model working against an unfinished contract only
produces values we would have to clean out later. See `app/llm_gate.py`.

Only *extraction* is manual. Sorting, normalisation and gap recomputation are the same code in both
modes (`airtable_client.upsert_*`, `app.gaps`), so hand-filled records obey the same canons the model
will obey later. Manual input goes through `tools_manual_intake.py`, never through ad-hoc PATCH calls —
a direct write bypasses select-option validation, coordinate order and `Key` format.

While disabled, extraction returns `status = "manual_required"` and **no verdict** about the source.
`is_relevant` stays `None`, never `False`: claiming a developer's message is irrelevant when nothing read
it is how data goes missing silently.

**Switching the API back on: `LLM_BACKEND=gemini` + `GEMINI_API_KEY` in `.env`, no code edits.** The full
checklist — what was disabled, how to verify it is live, and what to expect on the first run with a warm
cache — is [`LLM_COMEBACK.md`](../../LLM_COMEBACK.md). Keep that file current: it is the only place the
reason for the shutdown is written down.

The parser opens each source and extracts fields per the contract in `gemini_parser.EXTRACTION_PROMPT`.
That prompt stays the single description of the contract in both modes: whoever fills a field by hand
follows the same rules, so switching the API back on does not change the meaning of what is stored.
A JS-rendered page (Notion) yields nothing to a plain fetch — it must be read through the browser.
A short Maps link (`maps.app.goo.gl`) carries no coordinates: resolve the redirect to the full
`.../@lat,lng` form, then swap to `"lng, lat"` for `Coordinates(for Map)`.

### Step 3 — Mirror the renders to our Drive
Renders found in the developer's materials are copied to **our** Drive under `GDRIVE_MIRROR_ROOT_ID`
(`drive_mirror.mirror_project_drive_files`), never merely linked.

**Mirror layout mirrors the base: `/{Developer}/{Project}/{Unit Type}/{set}/…`** (owner, 06.08.2026), and
below that the source's own folder names are preserved. Two rules make it usable:
- Never flatten. When one unit type has several render sets in the source ("1BR: 1-7 | 8-14 | 15-22"),
  each goes to its own `subfolder`. Merged into one folder, files with colliding names are dropped as
  already-present — that cost K-Village 6 renders on 06.08.2026 before `subfolder` existed.
- Traversal depth comes from `GDRIVE_MAX_DEPTH` (default 8, ceiling 12). The old hard limit of 5 silently
  truncated listings for developers whose folders nest deeper — Y-WAY's kit goes six levels down.

Field targets:
- `Projects.Renders` → the mirror folder **on our Drive**. Writing the developer's own folder here is a
  bug: their link can die or change, and the next run would re-download our own copy as if it were the
  source. The developer's originals belong in `Link to Developer’s Kit (Rus/Eng)`.
- `Units.Renders` → the mirror folder of **that unit type**, not the project's.
- `Units.Plan Link` → the unit type's floor plans (a folder, matching how the field is already used).
- `Img` → cover, chosen by `drive_mirror.COVER_PRIORITY`: the widest shot of whatever the record is
  about. For a project that is the complex (masterplan, aerial, bird view), for a unit its own exterior —
  never the complex plan on a unit record. Interiors rank below; drawings and floor plans rank last and
  are used only when there is nothing else. Keywords are matched against the **folder path**, not the
  file name: developers name files `1.jpg`, `4.jpg`, `Бомба.jpg`, and only the folder carries meaning.

**`Active` protects filled values, not the whole record** (owner, 08.08.2026). While the box is
ticked, an existing value is never overwritten and the service fields (`Status`, `Gaps`,
`Last updated`, `Source`) are not written at all — but empty fields are still filled in and missing
units are still created. Blocking creation protected nothing: Axis One and Y-WAY had no units at all,
so there was nothing to overwrite, yet every run skipped them and both sat empty until the boxes were
cleared by hand. Only a human may set or clear the box (`HUMAN_ONLY_FIELDS`) — a lock the bot can open
guarantees nothing.

**The availability chart wins, and the latest handover date wins** (owner, 08.08.2026). When sources
disagree on a figure, the developer's availability chart beats the brochure, the SUMMARY sheet and the
financial model — model tabs named `2BR_430k / 450k / 500k` are yield SCENARIOS, not a price list, and
reading the lowest as "the price" understated Flower Estates by $70 000. Handover dates are the one
exception to "chart wins": construction slips, never accelerates, so take the **latest** date any source
gives — Flower Estates read Q1 2027 in the SUMMARY, Q2 2027 in the chart and Q3 2027 in the Nuanu
catalog, and Q3 2027 is what goes in the record.

### Step 4 — Sort into the tables, then close the gaps
Records are written in dependency order — **Developer → Projects → Units / Units (Secondary)** — so links
resolve (`airtable_client`). The split between the two unit tables is **who sells**, not what stage the
building is at (owner, 08.08.2026): `Units` is the primary market — stock sold by the developer, a unit
that has never had an owner. `Units (Secondary)` is the resale market — the same typology, the same
unit, but one that somebody already bought and is now reselling. "Off-plan" is not the test: a finished
project's unsold stock is still primary, and a resold unit in an unfinished project is still secondary.
A secondary record must never overwrite a primary one.

Both tables hold TYPOLOGY, not physical lots (see "Units holds TYPOLOGY" above) — the market is what
differs between them, not the granularity.

**A sold-out typology stays in `Units`** with `Availability = Sold`; it is not moved anywhere (owner,
08.08.2026). It is still primary-market data — it records what the project is made of, and the
developer's price is the baseline a later resale ask is judged against. Moving it to
`Units (Secondary)` would assert a resale that nobody is offering. The interface hides `Sold`
typologies by **status**, not by removing the record. When an owner does resell, a record is
**added** to `Units (Secondary)` with the new asking price, and the primary record stays as history.

Known limitation, accepted for now (owner, 08.08.2026): a secondary record is still a typology, not a
concrete offer — one physical lot with its own price, floor and terms. Representing real offers needs
a separate mechanism (likely per-offer records, possibly marked by hand). Not a priority yet, but do
not design around the assumption that a secondary row equals one lot.

Prices are "from" values, so one typology absorbs a spread across lots. Keep it one record and write
down why: Y-WAY's Deluxe appears as 57.69 m² and 69.51 m², but the difference is terrace size — the
internal area is identical, so it is a single typology whose `Area from` and `Price from` are the
minimums (owner, 08.08.2026).

Then `gaps.compute_project_gaps()` recomputes what is still empty **from the record**, not from the
model's own account of what it missed, writes the list to `Gaps` and sets `Status = Needs data`. Every
gap is retried against the remaining sources before the project is reported as incomplete.

**Fallback for anything the materials do not cover.** A field missing from the dev kit and the Drive
folders is looked up in the **last 100 messages of the source chat**
(`history_scanner.scan_chat_metadata_and_history(client, chat, limit=100)`) before it is declared a gap.
Developers routinely answer in chat what their materials never state — permits, a construction stage, a
shifted handover date. Chat text ranks below the availability chart but above the materials (§2), so a
newer answer in the group supersedes an older PDF.

A field stays empty rather than guessed. Minutes on foot are not metres; a prestigious district is not a
zoning colour. What survives step 4 is a genuine question for the developer — the future outreach tool
will send exactly this list.

## 5. Safety & Operations
- STRICTLY PROHIBITED: Deleting files/directories without a git tracking check or backup.
- Always run tests before declaring a task completed: `python -m pytest tests/ -q`.
- Reproduce a bug with a failing test **before** fixing it.
- Verify against the real resource, not in-memory flags: no `pool._closed`, no `client.is_connected()`.
- Never swallow an error with a bare `try/except` that neither logs nor surfaces a failure status.
- A passing unit test proves nothing if nothing calls the code. New code must be reachable from an
  entry point, and `tests/test_wiring.py` guards both the wiring and the absence of dead exports.
- Airtable records are archived via the `Active` checkbox, never deleted.
- Temporary media downloaded from links is deleted immediately after extraction.
