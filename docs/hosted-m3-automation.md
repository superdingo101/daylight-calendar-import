# M3 opt-in service-path evidence runner

This is a **separate supplemental acceptance tool** to [the manual New import rehearsal](hosted-m3-live-acceptance.md). It does not prove the browser UI worked, that Cloud used particular PDF bytes, or that model output is deterministic.

The normal `plan` command checks the **ten previously pinned fixtures** with no network or provider calls:

```bash
python scripts/m3_acceptance.py plan
```

Run only on a reviewed checkout of `integration/hosted-v1` in an isolated operator-controlled disposable HA environment. The CLI **intentionally allows only HTTP loopback** (for example `http://127.0.0.1:8124`); if the script runs outside the HA host, use a protected SSH local port forward instead of expanding network access. Do not run through public Home Assistant. Set a temporary high-privilege *disposable HA* token through a protected environment variable, never on the command line or in Git:

```bash
umask 077
export DAYLIGHT_M3_HA_TOKEN='(load via protected environment, not shell history)'
python scripts/m3_acceptance.py run --confirm \
  --ha-origin http://127.0.0.1:8124 \
  --output /protected/m3-ha-run-unique \
  --case text_basic --case text_zero --case text_all_day \
  --case text_meeting --case text_dst --case text_mixed \
  --case text_long --case image --case pdf_text --case pdf_scan
```

**Authorization is separate from the CLI flag.** Before running, verify the exact HA/Cloud/Infrastructure revisions, the deployed Cloud `/ready`, an unexpired installation-specific internal parse grant, a reviewed model-call count/spend budget, and isolated synthetic-only calendar and review state. This command performs real service submissions and can cause paid model calls. `--confirm` merely prevents accidental execution; it is not an access grant or budget control. The script does not create grants or change Railway settings.

The runner exercises the integration's `submit_text`, `submit_image`, `submit_pdf` and `get_pending` services. For media, it uploads through HA's documented `/api/file_upload` endpoint. It uses distinct source IDs, does not approve drafts, and stores only issue categories, result dispositions, pending identifiers and timestamps. It compares expected dates/times, count, titles, locations, descriptions and long-description step/tail coverage. Model-dependent extraction mismatches are marked `quality_mismatch`, **not silently corrected**.

Evidence is written into an exclusively created 0700 directory, with a 0600 `evidence.json`. The tool does not automatically retry an uncertain submission; inspect the review inbox and Cloud diagnostic records before deciding anything further. Runs are deliberately not resumable. If the process is interrupted, treat any unfinished row as uncertain.

To verify persistence, independently restart the *disposable* HA system using its normal safe operator procedure; record the actual restart, HA revision and time outside the tool. Then run:

```bash
python scripts/m3_acceptance.py verify --confirm \
  --ha-origin http://127.0.0.1:8124 \
  --evidence /protected/m3-ha-run-unique/evidence.json \
  --output /protected/m3-ha-readback-unique
```

The verification only proves that pending IDs were still retrievable at the later time. It does **not** automatically attest a restart; `restart_was_observed` remains false until independently recorded. The no-events outcome has no pending ID and needs separately reviewed activity evidence.

For complete M3 acceptance, also record a before/after **read-only calendar snapshot** (zero unapproved calendar writes), normal browser New import observations, BYO regression, authorization-negative tests, Cloud model/provider and measured economics evidence, and Infrastructure staging evidence. An automated service-path pass does not prove any of these. Keep raw source content, private credentials, provider responses, and private operational identifiers out of public issues and CI artifacts.

The local-only CLI is **not run in CI**. Unit tests exercise its offline logic only. Retain the separate manual acceptance document as the ultimate scenario list. Do not merge any hosted functionality into public `main` through this PR.
