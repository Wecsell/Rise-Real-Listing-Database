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
- Units are tracked **by type** ("Studio with pool", "Villa Sunrise 1BR"), not per physical unit.
  Availability is `On sale` / `Sold` for the whole type.
- `Field Staging.Priority` accepts only `Hight` / `Medium` / `Low`. `Hight` is a typo in the base,
  but it is the real option name. Russian values are rejected — normalise via
  `priority_parser.to_airtable_priority()`.
- Source hierarchy when data conflicts: availability chart (шахматка) > developer's answers in chat >
  their materials. Master record precedence: Telegram group > field finding or research.
- A finding reaches Developer/Projects/Units **only** after the `Confirmed` checkbox is ticked.
  Field data is a hypothesis until then.
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

## 4. Safety & Operations
- STRICTLY PROHIBITED: Deleting files/directories without a git tracking check or backup.
- Always run tests before declaring a task completed: `python -m pytest tests/ -q`.
- Reproduce a bug with a failing test **before** fixing it.
- Verify against the real resource, not in-memory flags: no `pool._closed`, no `client.is_connected()`.
- Never swallow an error with a bare `try/except` that neither logs nor surfaces a failure status.
- A passing unit test proves nothing if nothing calls the code. New code must be reachable from an
  entry point, and `tests/test_wiring.py` guards both the wiring and the absence of dead exports.
- Airtable records are archived via the `Active` checkbox, never deleted.
- Temporary media downloaded from links is deleted immediately after extraction.
