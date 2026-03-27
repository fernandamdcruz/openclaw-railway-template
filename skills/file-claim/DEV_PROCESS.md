# Claim Filer Development Process

## MANDATORY rules for anyone (human or AI) editing claim filing code

### Before touching ANY code

1. **Read the current file first.** Not from memory. Not from a summary. `cat` the actual file and read what's there NOW.
2. **Read FERDY_README.md** for current operational context.
3. **Understand the architecture:**
   - `claim_filer_api.py` = primary. Uses direct HTTP API calls. Logs in via Playwright ONLY to get OAuth token.
   - `claim_filer.py` = legacy fallback. Full Playwright browser automation. Only use if API approach is fundamentally broken.
   - SKILL.md tells FerdyBot which to run.

### Before committing ANY change

1. **Syntax check:** `python3 -c "import py_compile; py_compile.compile('skills/file-claim/claim_filer_api.py', doraise=True)"`
2. **Dry run test:** `python3 -c "from skills.file_claim.claim_filer_api import *; print('imports OK')"` (or equivalent)
3. **If you added a new function**, write a 3-line test that calls it with sample data and verify it doesn't crash.
4. **If you changed the spreadsheet column mapping**, print the actual row data from the sheet first to verify your assumptions.
5. **Bump SCRIPT_VERSION** so we can verify the deployed code matches.

### After deploying

1. **Verify the new code is actually running.** Check for SCRIPT_VERSION in logs.
2. **If FerdyBot doesn't use the script,** the deploy may not have copied files yet. The entrypoint.sh copies `/app/skills/*` to `/data/workspace/skills/` on container startup — a deploy without restart won't update the files.

### Things that have gone wrong before (DO NOT REPEAT)

| Mistake | What happened | Rule |
|---------|--------------|------|
| Assumed API has no auth | 403 error, wasted a deploy cycle | Always test API calls before building a whole script around assumptions |
| Used `re.search` for 2FA codes | Grabbed oldest code instead of newest | Use `re.findall` + take last match when searching ordered data |
| Changed `messages[-1]` to `messages[0]` without evidence | Made 2FA worse | Never flip logic without diagnostic data proving the current logic is wrong |
| Typed diagnosis into provider field | Combobox fallback grabbed wrong element | Always verify element locators with diagnostic dumps before automating |
| Selected wrong patient (Mathias instead of Fernanda) | Dropdown didn't filter properly | Never trust Flutter dropdowns to filter by keyboard input |
| Assumed deploy was live | FerdyBot ran old code | Always verify SCRIPT_VERSION in logs |
| Assumed column positions without checking | city="2026", country="Medical" | Always dump raw row data before changing column mappings |
| Built on API assumption without testing | Whole script built around no-auth, had to rebuild | Test the core assumption FIRST with a minimal script before building 1000 lines |

### The golden rule

**If you haven't tested it, it doesn't work.** Don't commit hope. Commit evidence.
