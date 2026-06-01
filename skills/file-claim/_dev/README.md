# Dev utilities for file-claim

Scripts here are for **manual debugging only** — they are not part of the production claim-filing flow. They get deployed to `/data/workspace/skills/file-claim/_dev/` on the volume but are never triggered by FerdyBot.

## `test_file_claim.py`
End-to-end smoke test of the claim filer. Run from a Railway SSH session:
```bash
cd /data/workspace/skills/file-claim
BCBS_TOKEN=<token> python3 _dev/test_file_claim.py
```

## `gog_diagnostic.py`
Diagnoses `gog` CLI auth / config issues (Google Sheets, Drive, Gmail). Run when claim filing fails on Sheets access:
```bash
XDG_CONFIG_HOME=/data/workspace/.config python3 _dev/gog_diagnostic.py
```
