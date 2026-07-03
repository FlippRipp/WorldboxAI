# Bundled sqlite-vec loadable extensions (Android)

Official prebuilt `vec0` loadable extensions from the
[sqlite-vec v0.1.9 GitHub release](https://github.com/asg017/sqlite-vec/releases/tag/v0.1.9),
bundled because PyPI has no Bionic (Android/Termux) wheel for
`sqlite-vec` — `pip install` fails there with
"No matching distribution found".

They are loaded by `backend/engine/sqlite_vec_fallback.py` only when
`import sqlite_vec` fails; on every other platform the pip package from
`requirements.txt` is used and these files are inert.

SHA-256 of the release tarballs these were extracted from (verified
against the release's `checksums.txt`):

| Asset | SHA-256 |
|---|---|
| sqlite-vec-0.1.9-loadable-android-aarch64.tar.gz | `76f60d4d2d89d2e5070ef8f1868c52b140a10200dbe98b0c2ca7a4d02d483eaa` |
| sqlite-vec-0.1.9-loadable-android-armv7a.tar.gz | `637a4d38cbff2c46e296451381c25c062920455d03ddc1df955cfa0f7b5df3f0` |
| sqlite-vec-0.1.9-loadable-android-x86_64.tar.gz | `11e0b3db8b1386644966788c29c90d4f2f17689985b924e0ee57936d48e55cf7` |
| sqlite-vec-0.1.9-loadable-android-i686.tar.gz | `d1a75768502e1ab050828e1e993833c622d81808233bba988c7f266607f63581` |

sqlite-vec is dual-licensed MIT / Apache-2.0, © Alex Garcia.

When bumping the `sqlite-vec` pin in `requirements.txt`, re-download the
matching `loadable-android-*` release assets and update this table.
