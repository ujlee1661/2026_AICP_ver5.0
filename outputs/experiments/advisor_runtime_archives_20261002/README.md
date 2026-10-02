# Advisor run archives (2026-10-02)

The adjacent run directories contain the Git-tracked result files. Each run's
`.runtime/` state and six large logs excluded by `.gitignore` are stored here as
split `tar.zst` archives. `manifest.json` records the ordered parts, SHA-256
checksums, original file sizes, and original file checksums. Every part is below
GitHub's 100 MB file limit.

Restore one run from the repository root, replacing `RUN_ID` with the directory
name:

```bash
archive_dir=outputs/experiments/advisor_runtime_archives_20261002
run_dir=outputs/experiments/RUN_ID
cat "$archive_dir/RUN_ID.runtime.tar.zst.part-"* | zstd -d -c | tar -xf - -C "$run_dir"
cat "$archive_dir/RUN_ID.ignored_logs.tar.zst.part-"* | zstd -d -c | tar -xf - -C "$run_dir"
```

`ADVISOR_GENERAL_20261001_TO_20260710` and
`ADVISOR_PERSONAL_20261001_TO_20260710` each completed 92 events through
2026-07-10 PM and passed `scripts/99_validate.py --allow-segment`. Their
validation reports are in `outputs/validation/`.

`ADVISOR_MAIN_CONT_OFF_20260925` is the copied historical OFF run. Its
checkpoint records all 92 events through 2026-07-10 PM as committed, and the
committed DB hash matches that checkpoint. The PM agent-turn, fill, and
community logs are present. `run_metadata.json` is stale at 91 events through
AM, and `segment_complete.json` is absent. Thus the PM event completed, while
final segment closeout and canonical validation were not recorded. The archive
preserves this state without modifying the original run.
