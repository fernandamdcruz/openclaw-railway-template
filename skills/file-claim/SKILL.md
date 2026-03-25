# File BCBS Claim — Skill Definition

## Description
Automatically file insurance reimbursement claims on the GeoBlue/BCBS member portal (https://members.bcbsglobalsolutions.com). Uses a hybrid approach: a Playwright Python script handles 90% of the workflow deterministically, and the LLM only intervenes for error recovery.

## Trigger
Say "file claim", "file my claims", "submit reimbursement", "BCBS claim", or similar.

## ⚠️ PRIMARY METHOD: Run the Playwright Script

**ALWAYS try the Playwright script first.** It files claims without using any LLM API calls, making it ~10x cheaper and ~4x faster than LLM-driven browser automation.

### How to run:
```bash
python3 /data/workspace/skills/file-claim/claim_filer.py
```

### What the script does automatically:
1. Reads pending claims from Google Sheets (column K = "Pending")
2. Logs into the BCBS portal (handles 2FA by reading the code from Gmail)
3. Navigates the 6-step claim wizard for each pending claim
4. Uploads supporting documents from Google Drive
5. Updates the Google Sheet with the claim reference number
6. Sends a Telegram notification to Fernanda

### When to fall back to LLM-driven browser automation:
Only use the manual browser automation approach (described below) if:
- The script exits with an error (check the output for `[ERROR]` lines)
- The script reports it got stuck on a specific step
- The portal UI has changed and selectors no longer match
- You need to handle a special case the script doesn't cover

If falling back, read the script's error output to understand WHERE it failed, then use the Flutter Web patterns below to complete just the remaining steps manually. Do NOT restart from the beginning.

## ⚠️ Token Efficiency Rules (MANDATORY — for LLM fallback only)

Each API call resends the full conversation context. Browser automation tasks make 50-100+ calls, so context size directly drives cost. Follow these rules to keep claims under $7:

1. **Use `find()` instead of `read_page`** — `find("Next button")` returns just what you need. `read_page` dumps the entire accessibility tree (~3K tokens). Only use `read_page` when you genuinely don't know what's on the page.
2. **Minimize screenshots** — Only take screenshots at these moments:
   - After login (verify success)
   - After each wizard step transition (verify correct page)
   - When something goes wrong (debug)
   - Before submitting the final claim (verify data)
   - Do NOT screenshot after every click, field fill, or scroll.
3. **Be concise** — Don't explain what you're doing. Just do it. No "I'll now click the Next button" — just click it.
4. **Batch field fills** — Fill all fields on a page, THEN screenshot once to verify. Don't screenshot between individual fields.
5. **Don't repeat tool calls** — If `find("Start Date")` found the element, click it immediately. Don't call `find` again or `read_page` to "confirm."

## ⚠️ CRITICAL: Flutter Web Automation Strategy

The BCBS portal is built with **Flutter Web using CanvasKit rendering**. This means:
- The page is composed of `<flt-semantics>` custom elements with ARIA roles (`role="button"`, `role="textbox"`, etc.)
- **DO NOT use coordinate-based clicks** — they break when the layout shifts
- **DO NOT use CSS selectors for standard HTML elements** — Flutter doesn't use `<input>`, `<button>`, etc. in the normal sense
- **USE the accessibility tree** — `read_page` with `filter: interactive` gives you all actionable elements with ref IDs

### How to interact with Flutter Web elements:

**Finding elements (PREFERRED method):**
- Use `read_page` with `filter: interactive` to get all interactive elements with ref IDs
- Use `find` with a natural language query (e.g., `find("close button")`) to locate specific elements
- Click elements by their `ref` ID — this is more reliable than coordinates

**Buttons and links:**
- Elements appear as `button "Button Text"` in the accessibility tree
- Click by ref ID after finding with `read_page` or `find`
- After clicking, **always wait 1-2 seconds** for Flutter to rebuild the widget tree

**Text fields (including dropdowns):**
- Text fields appear as `textbox "Label text"` in the accessibility tree
- Dropdowns ALSO appear as `textbox "... dropdown"` — they are NOT `<select>` elements
- Step 1: **Click** the textbox element by ref
- Step 2: **Wait 500ms** — Flutter dynamically creates a real `<input>` inside `<flt-text-editing-host>`
- Step 3: For plain text fields: **Type** using keyboard — do NOT try to find the input element
- Step 3 (alt): For dropdowns: After clicking, wait 1s, then look for overlay options as new `button` elements and click the matching one
- Step 4: **Press Tab** to move to the next field (triggers Flutter's onSubmitted/unfocus)

**Date fields (Flutter Material DatePicker — CRITICAL):**

⚠️ **DO NOT** try to type dates into the searchbox/text input — `startDate.value = '...'` and even `document.execCommand('insertText')` do NOT trigger Flutter's internal `setState()` on the picker widget, so clicking OK saves nothing.

**USE THE CALENDAR UI DIRECTLY — this is the ONLY reliable method:**

1. **Click** the date picker button (e.g., `button "Start Date of Service date picker. Current value: not set"`)
2. **Wait 2 seconds** for the calendar dialog to fully render
3. **Navigate to the correct month/year:**
   - The calendar dialog shows a month/year header (e.g., "March 2026") — use `find` to locate it
   - Click `button "Backward"` to go back one month, or `button "Forward"` to go forward
   - Repeat until you reach the target month (e.g., for January 2026 when viewing March 2026, click left arrow twice)
   - Alternative: Click the month/year header text to open a year/month selector, then pick the year, then pick the month
   - After each navigation click, **wait 1 second** for the calendar to re-render
4. **Click the target day** — each day appears as a `button` in the accessibility tree (e.g., `button "9"` for the 9th)
   - Use `find("9")` or `read_page` to locate the day button
   - **Be careful with ambiguous numbers** — the calendar may show trailing days from the previous/next month. Use the accessibility tree to confirm the correct button.
5. **Click OK** — look for `button "OK"` in the dialog
6. **Wait 1 second**, then screenshot to verify the date was saved
7. The date picker button should now show the selected date (e.g., "Current value: 01/09/2026")

**PREFERRED: Use the mm/dd/yyyy text input (confirmed working 2026-03-25):**
The date picker dialog has a `textbox "mm/dd/yyyy"` with `type="search"` at the top. This is a REAL HTML input that triggers Flutter's state:
1. Click the date picker button to open the dialog
2. Wait 2 seconds
3. Click the `textbox "mm/dd/yyyy"` field
4. Wait 500ms
5. Type the date in MM/DD/YYYY format (e.g., "01/15/2026")
6. Click OK (may be unlabeled in accessibility tree — try `get_by_text("OK")`)
7. Wait 1 second to verify

**If the text input doesn't work, use calendar grid navigation (original approach).**

**If the calendar is hard to navigate (many months away):**
- Try the "switch to input" approach as a FALLBACK ONLY:
  1. Look for a pencil/edit icon button in the date picker dialog header (switches to text input mode)
  2. Click it, wait 1 second
  3. Click the text input field that appears
  4. Wait 500ms for Flutter to create the editing input in `<flt-text-editing-host>`
  5. Use keyboard: `Ctrl+A` (select all), then `type` the date in MM/DD/YYYY format character by character
  6. Press Tab or click elsewhere to trigger onChanged
  7. Click OK
  - If this still doesn't work, go back to the calendar click approach

**Radio buttons:**
- Appear as `radio` elements in the accessibility tree
- The label text may be separate from the radio element — use position/context to identify which is which
- Click the radio element by ref

**File upload:**
- Look for a hidden `<input type="file">` element on the page using JavaScript: `document.querySelector('input[type="file"]')`
- Use the `file_upload` tool with the ref of the file input and the local file path
- If no file input exists, use the `filechooser` event pattern in JavaScript

**Scrolling the form:**
- The form content may extend below the visible viewport
- The outer page does NOT scroll — the form is inside a **scrollable `flt-semantics` container**
- To scroll, use JavaScript: find the `flt-semantics` element with `scrollHeight > clientHeight + 50` and `clientHeight > 300`, then set `el.scrollTop += 500`
- After scrolling, call `read_page` again to get the newly visible elements

**Dismissing popups/dialogs:**
- The portal shows popup dialogs (e.g., "Important Update", "NOTICE") that block interaction
- Use `find("close button for dialog")` or look for `button "Close"` in the accessibility tree
- Click the close button, wait 1s, verify it's dismissed with a screenshot
- If the first click doesn't work, try clicking directly on the X coordinates or using JavaScript

**Navigation between form steps:**
- Look for `button "Next"` or `button "Save charge"` or `button "Back"` in the accessibility tree
- After clicking, **wait 2-3 seconds** for the next step to fully render
- Take a screenshot after each step transition to verify you're on the right page

**General rules:**
- After EVERY interaction, wait at least 1 second before the next action
- Take screenshots SPARINGLY — only at wizard step transitions and errors (see Token Efficiency Rules above)
- If an element isn't found, try scrolling down first (Flutter lazy-renders content)
- Flutter keyboard navigation works: Tab, Shift+Tab, Enter, Space, Arrow keys

## Workflow

### Step 0: Read pending claims from Google Sheets

**Sheet ID**: `1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk`
**Tab**: `2026`

Read ALL rows. Only process rows where column K (Claim Status) = "Pending".

For each pending row, extract:
| Column | Header | Maps to |
|--------|--------|---------|
| B | Patient Name | Wizard Step 2 (Basic Information): Patient dropdown |
| C | Provider Name | Wizard Step 4 (Charges): Select Provider dropdown |
| D | Date of Service | Wizard Step 4 (Charges): Start Date of Service + End Date of Service (same date for single-day visits) |
| E | Amount Billed | Wizard Step 4 (Charges): Charge Amount |
| F | Currency | Wizard Step 4 (Charges): Billed Invoice Currency dropdown |
| G | Diagnosis Codes | Wizard Step 4 (Charges): Condition or Diagnosis dropdown |
| H | Procedure Code | Wizard Step 4 (Charges): Service Description dropdown |
| I | Invoice # | Wizard Step 4 (Charges): Charge Nickname |
| L | Drive File Link | Supporting Document upload (after charges are saved) |

If any required field (B, C, D, E, F, L) is blank, **stop and ask Fernanda** before filing.

**Provider → Patient mapping** (do NOT mix up):
- CLINICA LIVIDI / Clínica Lividi Med → patient: **Elena Miranda** (NEVER Mathias)

### Step 1: Login

1. Navigate to `https://members.bcbsglobalsolutions.com`
2. Wait for page load (look for "Login" button in the accessibility tree)
3. Click the Username field (look for `textbox` containing "Username")
4. Wait 500ms, then type the username from env var `BCBS_USERNAME`
5. Press Tab
6. Wait 500ms, then type the password from env var `BCBS_PASSWORD`
7. Click the `button "Login"`
8. Wait 3 seconds for redirect
9. Screenshot to verify login success

### Step 1.5: Handle 2FA (if triggered)

If the portal shows a verification code screen:
1. The `claim_filer.py` script handles this automatically — it searches Gmail for the verification code using `gog mail list` (with IMAP as fallback)
2. If running manually (LLM fallback mode): use `gog mail list --query "from:bcbs verification code" --max 1` to get the latest code
3. Look for a 6-digit number in the email body
4. Enter the code into the `textbox` matching "code|otp|2fa|verif"
5. Click the submit/verify button

### Step 2: Navigate to eClaim Form

1. After login, look for the home dashboard
2. Use `find("eClaim")` or `find("File a Claim")` to locate the navigation element
3. Click it by ref and wait 3 seconds
4. Screenshot to verify you're on the claim form wizard
5. **Dismiss any popups** — the portal often shows an "Important Update" dialog. Find and click `button "Close"` or the X button.

### Step 3: Wizard Step 1 — Preliminary Questions

URL pattern: `.../webPreliminaryQuestions`

This step has 3 questions:

1. **"Who will receive the reimbursement?"** — Two buttons: `button "PRIMARY MEMBER"` and `button "PROVIDER"`. **Click PRIMARY MEMBER.**
2. **"Please choose the desired reimbursement method:"** (appears after selecting PRIMARY MEMBER) — Two buttons: `button "US DOLLAR CHECK"` and `button "BANK WIRE TRANSFER OR ACH PAYMENT"`. **⚠️ ALWAYS click BANK WIRE TRANSFER OR ACH PAYMENT. NEVER CHECK.**
3. **"Was the patient's treatment due to an accident or work-related injury?"** — Two radio buttons: Yes / No. **Click No.**
4. Click `button "Next"` to proceed.
5. Wait 2-3 seconds, screenshot.

### Step 4: Wizard Step 2 — Basic Information

URL pattern: `.../claimant`

Fields on this page:
- `textbox` **"eClaim Nick Name"** — Pre-filled with "CLM DD-MMM-YYYY". **Clear and replace** with the invoice number from Sheet column I.
- `textbox "Patient dropdown"` — **Click to open dropdown**, then select the patient name from Sheet column B. Must match EXACTLY as shown in the dropdown.
- `textbox` **Email** — Pre-filled, leave as-is.
- `textbox` **Phone** — Pre-filled, leave as-is.
- PRIMARY MEMBER INFORMATION section — All pre-filled/read-only (Country, Address, City, State, Zip, Employer). Do not touch.

After filling, a **NOTICE popup** may appear. Dismiss it by clicking `button "Close"`.

Click `button "Next"` to proceed. Wait 2-3 seconds, screenshot.

### Step 5: Wizard Step 3 — Other Insurance Form

URL pattern: `.../otherinsurance`

Simple page with one question:
- **"Is the patient covered under other health insurance?"** — Two radio buttons: Yes / No. **"No" is pre-selected by default.** Verify it's selected, don't change it.

Click `button "Next"` to proceed. Wait 2-3 seconds, screenshot.

### Step 6: Wizard Step 4 — Charges (Invoiced Charges)

URL pattern: `.../invoiceChargesForm`

**⚠️ This is the most complex step. The form is long and requires scrolling.**

The form content is inside a **scrollable `flt-semantics` container** (not the outer page). To scroll:
```javascript
// Find the scrollable container and scroll down
const els = document.querySelectorAll('flt-semantics');
let scrollable = null;
for (const el of els) {
  if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 300) {
    scrollable = el;
  }
}
if (scrollable) scrollable.scrollTop += 500;
```

Fields (in order from top to bottom):

**Section: Header**
- `textbox` **"Charge Nickname"** — Pre-filled with "CHG 1 DD-MMM-YYYY". **Clear and replace** with the invoice number from Sheet column I.

**Section: LOCATION DETAILS**
- **"Is this charge for a doctor or hospital?"** — Two radio buttons: "Doctor / Dentist / Pharmacy" (pre-selected) and "Hospital / Facility". **Leave as Doctor/Dentist/Pharmacy** unless the provider is a hospital.
- `textbox "Select Provider dropdown"` — **Click to open**, search/type the provider name from Sheet column C, select from results.
- `textbox` **"City"** — Type the city where the provider is located.
- `textbox "Country of Treatment dropdown"` — **Click to open**, select the country (e.g., "Brazil", "France").

**Section: CHARGE DETAILS** (scroll down to see)
- `textbox` **"Charge Amount"** — Type the amount from Sheet column E (numeric, e.g., "150.00").
- `textbox "Billed Invoice Currency dropdown"` — **Click to open**, select from Sheet column F. Values: BRL = Brazilian Real, EUR = Euro, USD = US Dollar.

**Section: VISIT DETAILS** (scroll down more to see)
- `textbox "Condition or Diagnosis dropdown"` — **Click to open**, search for the diagnosis code from Sheet column G. If not found in dropdown, select "OTHER" and type a description.
- `textbox "Service Description dropdown"` — **Click to open**, select the service from Sheet column H. If not found, select "OTHER" and describe.
- `button "Start Date of Service date picker..."` — **Click to open the calendar dialog. DO NOT type a date — use the calendar UI instead.** Navigate to the correct month using the left/right arrow buttons, then click the target day number, then click OK. See the "Date fields" section above for the full procedure. Parse the date from Sheet column D (format may vary — convert to month/day/year).
- `button "End Date of Service date picker..."` — **Same procedure as Start Date.** Click to open calendar, navigate to the same month, click the same day, click OK. For single-day visits, both dates should match.

After filling ALL fields, screenshot the completed form.

Click `button "Save charge"` (NOT "Next" — this step uses "Save charge"). Wait 2-3 seconds.

**After saving:** The portal may show a charges summary with the option to add more charges or proceed. If there are more pending charges for this claim, click "Add another charge" and repeat. When all charges are entered, click `button "Next"` to proceed to Step 5.

### Step 7: Wizard Step 5 — Reimbursement Details

URL pattern: `.../reimbursementdetails`

**⚠️ ALWAYS SELECT WIRE. NEVER SELECT CHECK.**

Note: The reimbursement METHOD (Wire vs Check) was already chosen in Wizard Step 1 (Preliminary Questions). This step is for selecting the specific bank account and currency.

- **Account**: Select the pre-saved US bank account on file.
- **Currency**: **USD**

Click `button "Next"` to proceed. Wait 2-3 seconds, screenshot.

### Step 8: Wizard Step 6 — Authorization

URL pattern: `.../authorization`

1. Read the authorization text (may require scrolling)
2. Check any required checkboxes/acknowledgments
3. Screenshot the authorization screen
4. Click `button "Submit"` or `button "File Claim"` or similar
5. Wait for confirmation (may take several seconds)
6. Capture the **claim reference number** from the confirmation page
7. Screenshot the confirmation page

### Step 9: Supporting Document Upload

After the claim is filed, upload supporting documents:
1. Look for a "Documents" or "Upload" section on the confirmation/claim detail page
2. Download the invoice file from the Drive link in Sheet column L
3. Upload using the file input (look for hidden `<input type="file">` on the page)
4. If no upload option is on the confirmation page, navigate to the claim details and look for a document upload area

### Step 10: Update Google Sheets

1. Update column K (Claim Status) from "Pending" to "Filed"
2. Add the claim reference number to column M (or next available column)

### Step 11: Notify Fernanda

Send via Telegram (chat ID: 8409634074):
```
✅ BCBS Claim Filed
Patient: [name]
Provider: [name]
Date: [date]
Amount: [amount] [currency]
Claim Reference: [ref number]
```

## Troubleshooting

### Popups blocking the form
- The portal shows dialogs ("Important Update", "NOTICE") that block interaction
- Use `find("close button")` to locate the dismiss button
- Click it, wait 1s, verify dismissed
- If clicking doesn't work, try `find("Close")` and click that ref instead

### "Element not found" errors
- Flutter lazy-renders content. **Scroll down first** using the JavaScript scroll pattern above.
- Wait longer (2-3 seconds) after page transitions.
- Try `read_page` with `filter: interactive` to see all available elements.
- Try Tab key navigation to cycle through elements.

### Date picker won't save the selected date
This is a KNOWN Flutter Web issue. The root cause: Flutter's DatePicker dialog manages its own internal state via `setState()`. Programmatic text input (setting `.value`, `execCommand`, `dispatchEvent`) does NOT call `setState()`, so the OK button saves nothing.

**Solution: ALWAYS use the calendar grid clicks.**
1. Open the picker, wait 2 seconds
2. Navigate to the target month using arrow buttons (each click = 1 month)
3. Click the day number button in the calendar grid
4. Click OK
5. Verify the date picker button text changed from "not set" to the expected date

**If the day buttons aren't visible in the accessibility tree:**
- Try `read_page` with `filter: all` (not just interactive) — some days may not have the `button` role
- Try `find("January 9, 2026")` or `find("9, Friday")` — Flutter may use full date labels
- As a last resort, use coordinate-based clicks on the visible day number in the calendar grid (take a screenshot first to locate the exact position)

**If you accidentally opened text input mode (pencil icon):**
- Click the calendar icon to switch back to calendar mode
- Then use the calendar click approach

### Dropdown won't open
- Click the `textbox "... dropdown"` element, wait 2 seconds, take screenshot.
- The overlay options appear as `button` elements — look for them in the accessibility tree after clicking.
- Some Flutter dropdowns respond to `page.keyboard.press('Space')` after focusing.
- If the dropdown is a search-type: after clicking, start typing to filter options.

### Text not entering in field
- Make sure you clicked the field first and waited 500ms.
- Check that the cursor/caret appeared (screenshot).
- Use keyboard typing (not `fill()`) — Flutter text fields need keystroke events.
- To clear a pre-filled field: Triple-click to select all, then type the new value.

### Page navigation fails
- The URL might not change (Flutter uses client-side routing).
- Look for visual changes (heading text, step indicator) rather than URL changes.
- Wait up to 5 seconds after clicking navigation buttons.
- **NEVER navigate directly by URL** — Flutter's client-side router doesn't support deep linking during a form session. Always use the form's Next/Back buttons.

### Scrolling doesn't work
- Do NOT use window scroll — the form is inside a nested `flt-semantics` scrollable container.
- Use the JavaScript pattern: find the container with `scrollHeight > clientHeight + 50`, then adjust `scrollTop`.
- After scrolling, call `read_page` again to refresh the element refs.

### File upload fails
- Look for a hidden `<input type="file">` element using JavaScript: `document.querySelector('input[type="file"]')`
- Use the `file_upload` tool with the ref if found via `read_page`
- If no file input exists, try the `filechooser` event pattern

## Data Reference

### Railway env vars required
- `BCBS_USERNAME` — BCBS member portal login email
- `BCBS_PASSWORD` — BCBS member portal password
- `XDG_CONFIG_HOME=/data/workspace/.config` — for gog Gmail access (2FA)
- `GOG_KEYRING_PASSWORD=ferdybot-calendar-2026` — for gog keyring

### Google Sheets
- Sheet ID: `1wU7iuAH7mZdenIKNAyrUFuJkVjZsYjxeL07NzqUwMYk`
- Tab: `2026`

### Telegram
- Chat ID: `8409634074`

## Notes
- Use browser profile `openclaw` (local Chromium), NOT `browserbase`
- The portal uses Flutter Web with CanvasKit rendering — all interactions go through flt-semantics accessibility elements
- All `flt-semantics` elements are real DOM nodes that can be queried and interacted with via the accessibility tree
- Take screenshots SPARINGLY per Token Efficiency Rules above
- If the form flow changes, the accessibility tree will still expose element roles and labels — adapt accordingly
- The 6-step wizard is: Preliminary Questions → Basic Information → Other Insurance → Charges → Reimbursement Details → Authorization
