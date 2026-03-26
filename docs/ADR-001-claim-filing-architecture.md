# ADR-001: Faster, Cheaper BCBS Claim Filing

**Status:** Proposed
**Date:** March 24, 2026
**Deciders:** Fernanda

## Context

FerdyBot currently files BCBS insurance claims by driving a browser via LLM (Claude Sonnet). Every click, every field fill, every navigation step requires an API call that sends the full conversation history. The result:

- **~65 API calls** per claim
- **~55K input tokens per call** (conversation context grows with each step)
- **~$11 per claim** in API costs
- **~20 minutes** end-to-end
- **Fragile** — if one step fails, the LLM often restarts from scratch

The root problem: **we're using an LLM to do what a script could do.** 90% of the claim filing workflow is deterministic — the same buttons get clicked in the same order every time. The LLM adds value only for reading data, handling errors, and adapting when the UI changes.

## Decision

**Hybrid approach: Script the deterministic steps, use the LLM only for decisions and error recovery.**

## Options Considered

### Option A: Keep Current Approach (LLM drives everything)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (already built) |
| Cost per claim | ~$11 |
| Speed | ~20 min |
| Reliability | Low (context window bloat, restart-from-scratch failures) |
| Adaptability | High (LLM can handle UI changes) |

**Pros:** Already working, flexible, handles unexpected UI changes
**Cons:** Expensive, slow, unreliable, wastes LLM on button clicks

### Option B: Playwright Script + LLM for Errors Only

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium (need to write Playwright script) |
| Cost per claim | ~$0.50-1.00 (LLM only called for errors/decisions) |
| Speed | ~3-5 min |
| Reliability | High (scripts don't lose context, can retry individual steps) |
| Adaptability | Medium (script breaks if UI changes, but LLM can be fallback) |

**How it works:**
1. A Python/Node script using Playwright handles all browser automation directly
2. Login → navigate → fill fields → click buttons are all scripted with explicit selectors
3. The script reads claim data from Google Sheets directly (via API, no browser needed)
4. The LLM is called ONLY when:
   - A selector fails (UI changed) — LLM examines the page and suggests new selector
   - 2FA code is needed — LLM reads Gmail
   - An unexpected popup appears — LLM decides how to dismiss
   - An error occurs — LLM diagnoses and suggests recovery
5. Result: maybe 3-5 LLM calls instead of 65

**Pros:** 10x cheaper, 4x faster, much more reliable, deterministic steps can't hallucinate
**Cons:** Requires coding the script upfront, breaks when BCBS changes their UI (but the LLM fallback handles this)

### Option C: Direct HTTP API (No Browser at All)

| Dimension | Assessment |
|-----------|------------|
| Complexity | High (need to reverse-engineer BCBS API) |
| Cost per claim | ~$0.00 (no LLM needed) |
| Speed | ~10 seconds |
| Reliability | High (but brittle to API changes) |
| Adaptability | Low (any backend change breaks it) |

**How it works:**
1. Intercept the network requests BCBS's Flutter app makes
2. Replay them directly via HTTP (login, create claim, add charges, submit)
3. No browser, no LLM, pure API calls

**Pros:** Fastest possible, zero LLM cost, completely deterministic
**Cons:** Hard to build, BCBS could change their API anytime, session/auth management is complex, may violate ToS

## Trade-off Analysis

**Option B is the clear winner.** Here's why:

- Option A (current) is too expensive and slow. At $11/claim with dozens of pending claims, costs add up fast.
- Option C is the theoretical ideal but impractical — reverse-engineering a Flutter Web app's API is fragile and potentially risky.
- Option B gives us **90% of Option C's speed benefits** with **much less risk**. The Playwright script handles the happy path, and the LLM provides a safety net for the unexpected.

The key insight: **the LLM should be the exception handler, not the driver.**

## Implementation Plan

### Phase 1: Playwright Script (replaces 90% of LLM calls)

Write a Python script using Playwright that:

```
claim_filer.py
├── read_sheets()         # Google Sheets API — get pending claims
├── login()               # Navigate, fill username/password, submit
├── handle_2fa()          # Call read_2fa.py, enter code
├── dismiss_popups()      # Close known popup patterns
├── step1_preliminary()   # Click PRIMARY MEMBER, WIRE, No accident, Next
├── step2_basic_info()    # Fill eClaim name, select patient, Next
├── step3_insurance()     # Verify No selected, Next
├── step4_charges()       # Fill all charge fields, use calendar for dates, Save
├── step5_reimbursement() # Select bank account, USD, Next
├── step6_authorization() # Check boxes, Submit
├── upload_document()     # Download from Drive, upload to portal
├── update_sheets()       # Mark as Filed, add reference number
└── notify_telegram()     # Send confirmation
```

Each function:
- Uses explicit CSS/ARIA selectors for Flutter elements
- Has built-in waits and retries
- Takes a screenshot on failure and calls LLM for diagnosis

### Phase 2: LLM Error Recovery (the smart fallback)

When a Playwright step fails (selector not found, unexpected state):
1. Script takes a screenshot
2. Calls Claude with JUST the screenshot + "Expected to find X but got this. What should I do?"
3. Claude responds with a fix (new selector, click coordinates, etc.)
4. Script retries with Claude's suggestion

This keeps each LLM call to ~2K input tokens instead of 55K.

### Phase 3: Prompt Caching (immediate win for current approach)

While building Phase 1, we can immediately cut costs 50% with **Anthropic's prompt caching**:
- The skill definition (~15K tokens) is the same every call — cache it
- Cached input tokens cost 90% less ($0.30/M instead of $3/M)
- This cuts per-claim cost from ~$11 to ~$6 with zero code changes

## Cost Comparison

| Approach | LLM Calls | Cost/Claim | Time | Reliability |
|----------|-----------|------------|------|-------------|
| Current (LLM drives all) | ~65 | ~$11 | ~20 min | Low |
| Current + prompt caching | ~65 | ~$6 | ~20 min | Low |
| Playwright + LLM fallback | ~3-5 | ~$0.50-1 | ~3-5 min | High |
| Direct HTTP API | 0 | $0 | ~10 sec | Medium |

## Consequences

**What becomes easier:**
- Filing claims becomes fast and cheap enough to run daily
- Multiple claims can be filed in sequence without context window issues
- Failures are isolated to individual steps, not the whole flow

**What becomes harder:**
- Need to maintain the Playwright script when BCBS changes their UI
- Two codebases to maintain (script + skill definition)

**What we'll need to revisit:**
- The Flutter selectors when BCBS updates their portal
- Whether to move to Option C once we understand the API patterns better

## Action Items

1. [ ] **Immediate**: Enable prompt caching on FerdyBot's API calls (cuts cost 50% today)
2. [ ] **This week**: Write `claim_filer.py` Playwright script for the happy path
3. [ ] **This week**: Add LLM error recovery hooks to the script
4. [ ] **Next week**: Test with a real claim filing
5. [ ] **Ongoing**: Monitor for BCBS UI changes and update selectors
