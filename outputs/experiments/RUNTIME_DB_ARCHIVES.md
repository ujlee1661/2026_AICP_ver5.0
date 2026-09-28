# Runtime DB archives

The uncompressed `.runtime/runtime_sim.db` files are excluded from Git because
of their size. The corresponding `runtime_sim.db.zst` file in each run
directory is a byte-for-byte compressed copy of that SQLite database.

Restore a database with:

```bash
zstd -d runtime_sim.db.zst -o runtime_sim.db
sqlite3 runtime_sim.db 'PRAGMA quick_check;'
```

| Run | Original bytes | Archive bytes | Original SHA-256 | Archive SHA-256 |
| --- | ---: | ---: | --- | --- |
| `ADVISOR_MAIN_FROM_SCRATCH_PARENT_20260923` | 392384512 | 21161490 | `8b003e1bee937164b30987cdc9fb5c09d612b74b90a6f1636c6d219d04e8cd5d` | `5e1b50e1ba77878876cdb1f737752837d942ce566deeaae2a80607d1ceb7d6e2` |
| `ADVISOR_MAIN_CONT_OFF_20260925` | 796086272 | 42630762 | `5ad15074e267a0c83d95c8242c302536b553af228280ab8865a66cb08a71e6d6` | `bbab7fca4708e4dc4a2941c600fef4e74e0194ff07e08ad8ab3fc38e7baa84b8` |
| `ADVISOR_MAIN_CONT_ON_20260925` | 796581888 | 42775298 | `c339e3ecae94fb99a4f3dc1d1cf63969af920dfc92b03649a5f8b2ec18b2ed3b` | `bbb332fa13f7b94e495387b62a53133725d46e287f3fb2c1d7df3802cab43dae` |

All three source databases passed SQLite `PRAGMA quick_check`, and each Zstandard
archive passed `zstd -t` before being committed.
