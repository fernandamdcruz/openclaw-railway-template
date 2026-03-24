# File BCBS Claim — Skill Definition

## Description
Automatically file insurance reimbursement claims on the GeoBlue/BCBS member portal (https://members.bcbsglobalsolutions.com). Uses accessibility-based selectors for Flutter Web.

## Trigger
Say "file claim", "file my claims", "submit reimbursement", "BCBS claim", or similar.

## ⚠️ CRITICAL: Flutter Web Automation Strate

The BCBS portal is built with **Flutter Web using the HTML renderer** (NOT CanvasKit canvas). This means:
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

**Date fields:**
- Date pickers appear as `button "Start Date of Service date picker. Current value: not set"`
- Click the button, wait for the date input to appear
- Type the date in **MM/DD/YYYY** format
- Press Tab to confirm

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
- Take screenshots frequently — after each step, before and after filling fields
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
1. Run the 2FA reader: `python3 /data/workspace/read_2fa.py`
2. This script uses `gog mail list` to search Gmail for the latest BCBS verification code
3. It retries for up to 90 seconds waiting for the email
4. Enter the code into the verification field
5. Submit

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
- `button "Start Date of Service date picker..."` — **Click**, then type the date from Sheet column D in **MM/DD/YYYY** format. Press Tab.
- `button "End Date of Service date picker..."` — **Click**, then type the SAME date (for single-day visits). Press Tab.

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
- The portal does NOT use CanvasKit canvas rendering — it uses Flutter HTML renderer with semantic DOM elements
- All `flt-semantics` elements are real DOM nodes that can be queried and interacted with via the accessibility tree
- Take screenshots at EVERY step for debugging
- If the form flow changes, the accessibility tree will still expose element roles and labels — adapt accordingly
- The 6-step wizard is: Preliminary Questions → Basic Information → Other Insurance → Charges → Reimbursement Details → Authorization
