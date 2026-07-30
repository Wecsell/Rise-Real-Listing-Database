# Workspace Rules: Rise Real Bali Listing Database

## 1. Stack & Architecture
- Python 3.11, Docker, asyncio.
- Pipeline: Telegram Userbot -> Postgres -> Airtable.
- Main logic resides in `/app`. Always run via `docker-compose`. Never modify `init.sql` without explicit approval.

## 2. Airtable Environment & Base IDs
The active base is determined dynamically by the `AIRTABLE_BASE_ID` environment variable in `.env`:
- **TEST Base ID**: `appsAbRs7DnYYWFt6` ("Base RR New Test") — *Active by default during development & current MCP connector*.
- **PROD Base ID**: `app2IEMPr6R3GelVP` ("Base RR New") — *Production deployment*.

### Table Mapping:
- **Projects**: `tbl15zdeaF04TLXSe` (PROD) / Name `"Projects"` (MCP)
- **Units**: `tblutK0qMdyPOjidT` (PROD) / Name `"Units"` (MCP)
- **Developer**: `tblhtsoZ8HXdU61fc` (PROD) / Name `"Developer"` (MCP)

### Business Canons:
- Key format: `project__unitno__Nbr` or `project__type__Nbr__views` (STRICTLY NO PRICES in Key!).
- Prices strictly in USD (number).
- Never modify formula fields (`Unit ID`, `Price per m²`).
- Image URL format: `https://drive.google.com/thumbnail?id={FILE_ID}&sz=w2000`.

## 3. Safety & Operations
- STRICTLY PROHIBITED: Deleting files/directories without git tracking check or backup.
- Always run tests before declaring task completed.
