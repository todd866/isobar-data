# Ask Isobar library search

Read-only search of the aviation and meteorology subset in PaperLibrary, through
the same LocalEvidence retrieval endpoint md3's tutor uses. The endpoint runs
no model. This server adds no network beyond that local HTTP call.

```sh
uv run python -m isobar_data.library_mcp
```

`ISOBAR_LIBRARY_URL` defaults to `http://127.0.0.1:8765`.
`ISOBAR_LIBRARY_COLLECTION` defaults to
`~/Projects/PaperLibrary/collections/aviation-meteorology.jsonl`.
The endpoint must be plain HTTP on `localhost`, `127.0.0.1`, or `::1`; this
adapter does not follow redirects and caps a response at 2 MiB.

| Tool | Purpose |
| --- | --- |
| `library_search(query, k=5)` | Post the query to `/api/ask` and return up to `k` passages whose slug is in the aviation collection. Each row has `title`, `source`, `page`, `section`, `licence`, and `passage`. |

A passage outside the collection is dropped. A prior worked answer with no
evidence is not returned as a passage. Page and section are whatever the
endpoint sent; this service does not invent them. Licence and source URL come
from the collection file. Results include `provenance`: `source-matched-excerpt` only
when the endpoint's source URL matches the collection URL; otherwise the text
is explicitly labelled `unverified-excerpt`. Matching URLs do not independently
verify the passage text. Environment HTTP proxies are bypassed.

Rebuild the PaperLibrary indexes, after a snapshot, with `python3 maintain.py`
in `~/Projects/PaperLibrary`. That refreshes the manifest, embeddings, full-text
index, and graph. It does not rewrite the collection file.
