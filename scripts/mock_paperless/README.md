# Mock Paperless-ngx server

A single-file, standard-library-only Python server that behaves enough like
Paperless-ngx (2.15+ / 3.x API) to run the app against a library of
25,000–100,000 documents: login, element sync, paged document lists, filters
and search, document detail, metadata, notes, suggestions, PDF and thumbnail
downloads, bulk OCR text, and simple mutations.

```sh
python3 scripts/mock_paperless/server.py                       # 25,000 documents on 127.0.0.1:8765
python3 scripts/mock_paperless/server.py --documents 100000 --quiet
```

In the app (Simulator): server `http://127.0.0.1:8765`, any username and
password, or the token `mock-token`.

All state is in memory. Restart the server to get the pristine library back.

## Options

| Flag | Default | |
| --- | --- | --- |
| `--host` / `--port` | `127.0.0.1` / `8765` | |
| `--documents N` | `25000` | ids `1..N` |
| `--seed S` | `1` | everything generated derives from it |
| `--pdf-kb K` | `40` | each PDF is 50–150 % of this, fixed per document; `0` = text only |
| `--latency-ms MS` | `0` | added to every `/api/` response (also settable at runtime) |
| `--token T` | `mock-token` | `""` disables authentication |
| `--server-version V` | `3.5.0` | sent as `X-Version`; gates version-dependent shapes (below) |
| `--api-version N` | `10` | sent as `X-Api-Version`; higher `Accept: …; version=` get a 406 |
| `--reference-date D` | `2026-11-01` | "today" of the generated library, or `today` |
| `--null-ordering` | `postgres` | NULLs last when ascending (PostgreSQL) or first (`sqlite`) |
| `--no-gzip`, `--gzip-level` | gzip on, level 1 | JSON bodies are gzipped when the client accepts it |
| `--no-all-ids` | | omit the `all` id array from document list pages (see fidelity notes) |
| `--quiet` | | no per-request log line |

Unless `--quiet`, every request logs `METHOD path?query status bytes ms` to
stderr. Query parameters and endpoints the mock does not know are ignored /
answered with 404, logged **once** to stderr as `[mock] UNKNOWN …`, and
counted in `/__mock/stats`.

## Generated library

Deterministic from `(seed, id)`: the same seed gives the same document `id`
regardless of `--documents`. (ASNs are numbered in id order.)

- **Documents**: mixed German/English titles ("Rechnung Telekom März 2024",
  "Kontoauszug 2023-07", "Insurance policy renewal 2022", …) from 28 document
  kinds; `created` over 15 years up to the reference date (more recent ones
  more frequent), `added` within the last 6 years, `modified` ≥ `added`; ASN
  on ~30 %; 1–12 pages; owner admin 72 % / anna 20 % / none 8 %; ~4 % with
  explicit view/change permissions; ~11 % untagged; ~1.4 % in the inbox;
  custom fields on ~24 %; 1–3 notes on ~5 %.
- **Elements**: 200 correspondents, 64 tags (inbox tag `#1`, three levels of
  nesting: Finanzen → Steuer → Steuer 2024), 25 document types, 10 storage
  paths, 6 custom fields (monetary, date, boolean, string, select, document
  link), 7 saved views, users `admin` (#1, superuser, the logged-in user) and
  `anna` (#2), group `Familie` (#1).
- **OCR text** (`content`): 0.9–6.3 KB (average 2.9 KB) of letter-like
  German/English text: a per-document header (correspondent, address,
  reference number, date, customer number), 3–12 paragraphs drawn from a pool
  of ~600 shared paragraphs (topic-specific and general), and a footer (amount,
  IBAN, page count). Most words in the text are not in the title, e.g.
  `Haftpflichtversicherung` is in ~290 titles but ~5,900 texts (N = 25,000).
  ~200 rare words ("Wallbox", "Erbschein", "apostille", …) each occur in
  0.1–0.4 % of the texts.
- **Files**: `…/download/` and `…/preview/` return a real PDF 1.4 generated on
  the fly (Helvetica text: title plus the OCR text, `page_count` pages) padded
  with an unused stream of pseudo-random bytes. Archive and original differ
  (other producer, footer and size), their sizes and SHA-256 checksums are what
  `…/metadata/` and `versions[].checksum` report. ~5 % of the documents have a
  PNG "scan" as original (`mime_type: image/png`), ~3 % have no archive
  version (`archived_file_name: null`). Files depend only on `(seed, id)`,
  never on later edits, so metadata edits change `modified` but not the
  checksum, as on a real server.
- **Thumbnails**: 300×424 palette PNG, 1–2 KB: a header band coloured by id
  with the document number, grey "text lines" derived from the id.

### Needle documents

Fixed ids, present whenever `--documents` is at least the id. The words occur
nowhere else, for any seed.

| Needle | Where | Ids |
| --- | --- | --- |
| `zebrafink` | text only | 137, 4242, 17011 |
| `quittungsnummer-7781` | text only | 2718 |
| `Glühwürmchenstraße` | text only | 808, 15150 |
| `mondscheintarif` | text | 555, 1234, 7777, 12345, 19999 |
| `Mondscheintarif` | title (and text) | 1234 |
| `Xylophonwartung` | title only | 314, 9001 |

So a title search for `mondscheintarif` finds 1 document and a title+content
search 5; `Xylophonwartung` is found by title search but by no content search.
The needle line sits right after the header in ids 137, 2718, 808, 555 and
12345, inside the 550 characters a list page returns (`truncate_content`). In
4242, 15150, 1234 and 19999 it is in the middle of the text and in 17011 and
7777 at its end, so those are only found with the full text.

## Endpoints

Authentication is `Authorization: Token <token>`; without it `/api/` answers
401 (and, like the real server, sends no version headers). `POST /api/token/`
accepts any non-empty username and password; `GET /api/token/` answers 405, or
406 for an unsupported `Accept` version (the app's login probe).

| | |
| --- | --- |
| `GET /api/documents/` | paging (`page`, `page_size` ≤ 100000; `count`, `next`, `previous`, `all`, `results`), `fields=`, `full_perms`, `truncate_content`, `ordering`, filters below |
| `GET PATCH PUT DELETE /api/documents/{id}/` | PATCH/PUT: title, tags, correspondent, document_type, storage_path, archive_serial_number, created, owner, custom_fields, set_permissions, remove_inbox_tags; bumps `modified`. DELETE moves to the trash |
| `GET /api/documents/{id}/download/`, `…/preview/` | `original=true`, `version=`; `ETag` + `If-None-Match` → 304, `Content-Length`, `Content-Disposition` |
| `GET /api/documents/{id}/thumb/` | PNG; `ETag`/`Last-Modified` → 304 |
| `GET /api/documents/{id}/metadata/`, `…/suggestions/`, `…/share_links/`, `…/history/` | |
| `GET POST DELETE /api/documents/{id}/notes/` | DELETE with `?id=` |
| `POST /api/documents/post_document/` | multipart upload; adds a document at once and a `consume_file` task |
| `GET /api/documents/next_asn/`, `POST …/bulk_edit/`, `POST …/selection_data/` | bulk_edit: set_correspondent, set_document_type, set_storage_path, add_tag, remove_tag, modify_tags, delete, set_permissions, modify_custom_fields |
| `/api/tags/`, `/api/correspondents/`, `/api/document_types/`, `/api/storage_paths/`, `/api/saved_views/`, `/api/custom_fields/` | list (paged, `name__icontains`, `ordering`, `full_perms`), create, detail, PATCH/PUT, DELETE; `document_count`, `last_correspondence` are live |
| `/api/users/`, `/api/groups/` | read-only |
| `GET POST /api/ui_settings/` | |
| `GET /api/remote_version/`, `/api/statistics/`, `/api/config/`, `/api/profile/`, `/api/status/`, `/api/` | |
| `GET /api/tasks/`, `/api/tasks/{id}/`, `POST /api/tasks/acknowledge/`, `POST /api/acknowledge_tasks/` | |
| `GET POST /api/trash/` | `{"action": "restore" \| "empty", "documents": [...]}` |
| `POST /api/share_links/`, `DELETE /api/share_links/{id}/`, `GET /share/{slug}` | |
| `GET /api/search/autocomplete/`, `GET /api/search/`, `POST /api/bulk_edit_objects/` | |
| `GET /api/auth/headless/app/v1/config` | no OIDC providers |
| `/api/mail_accounts/`, `/api/mail_rules/`, `/api/workflows/`, `/api/logs/`, … | empty lists |

### Document filters

- Text: `title__icontains` (also `istartswith`, `iendswith`, `iexact`),
  `content__icontains`, `title_content`, `text`, `title_search`, `query`,
  `more_like_id`
- `id`, `id__in`
- Tags: `tags__id__all`, `tags__id__in`, `tags__id__none`, `tags__id`,
  `is_tagged`, `is_in_inbox`, `tags__name__icontains`
- `correspondent`, `document_type`, `storage_path`, `owner`: `…__id`,
  `…__id__in`, `…__id__none`, `…__isnull`, `…__name__icontains`
- ASN: `archive_serial_number` and `__gt`, `__gte`, `__lt`, `__lte`, `__isnull`
- Dates (`created`, `added`, `modified`): `…__date__gt/gte/lt/lte`,
  `…__gt/gte/lt/lte`, `…__year/month/day`
- Custom fields: `custom_field_query` (AND/OR/NOT, exists, isnull, exact, gt,
  gte, lt, lte, range, in, contains, icontains), `has_custom_fields`,
  `custom_fields__id__all/in/none`, `custom_fields__icontains`
- `mime_type`, `has_duplicates`, `shared_by__id`
- `ordering` (comma separated, `-` for descending): `id`, `created`, `added`,
  `modified`, `title`, `archive_serial_number`, `correspondent__name`,
  `document_type__name`, `storage_path__name`, `owner`, `notes`, `page_count`,
  `custom_field_<id>`, `score` (no-op)

Search semantics, all case-insensitive and exact with respect to the generated
texts (a phrase never matches across a line break):

- `title_content`, `title__icontains`, `content__icontains`: the whole value as
  a substring.
- `text`, `title_search` (3.0+): every whitespace-separated token as a
  substring.
- `query`: whole words, ANDed; `word*` prefix, `"a phrase"`, `-word` / `NOT`,
  `title:`, `content:`, `correspondent:`, `type:`, `tag:`, `path:`, `asn:`,
  and `created:` / `added:` / `modified:` with `[-3 month to now]`,
  `[2024-01-01 to 2024-12-31]`, `"today"`, `"yesterday"`, `"this month"`,
  `"this year"`, `"previous week|month|quarter|year"`. Relative ranges use the
  real clock, not `--reference-date`. Results carry `__search_hit__`.

### Version-dependent behaviour

| | |
| --- | --- |
| API ≥ 10 or server ≥ 3.0 | tasks in a paged envelope with the v10 shape, saved views without `show_on_dashboard`/`show_in_sidebar`, `versions` / `root_document` / `duplicate_documents` on documents, `text` and `title_search` filters, 400 for more than one of `text`, `title_search`, `query`, `more_like_id`, SHA-256 checksums (MD5 before) |
| server ≥ 3.2 | `has_duplicates` filter |
| server ≥ 2.19 | nested tags (`parent`, `children`, list returns the roots) |
| older | the parameters above are *unknown* and ignored, as on a real old server |

The `Accept` version is only validated; shapes follow the configured server.

## Control endpoints

Outside `/api/`, no authentication, never delayed or failed.

| | |
| --- | --- |
| `POST /__mock/fail?mode=503\|hang\|off` | all `/api/` requests answer 503 / are never answered / work. Also `500`, `502`, `401`, `403`, `429`. Optional `path=<substring>` limits it to matching request paths, `count=N` to the next N requests. Hung requests are dropped at the next `/__mock/fail` call |
| `POST /__mock/latency?ms=250` | change the artificial latency |
| `GET /__mock/stats` | requests by endpoint class and status, bytes served, PDFs and thumbnails served, 304s, hung requests, unknown parameters and endpoints |
| `POST /__mock/touch?id=5&title=New` | server-side edit: bumps `modified`; optional `title`, `content`, `tags=1,2`, `correspondent`, `document_type`, `storage_path`. `id` takes lists and ranges (`id=1-500,900`) |
| `POST /__mock/delete?id=5` | server-side delete (to the trash; `hard=1` skips it) |
| `POST /__mock/add?count=10` | append new documents (inbox tag, `added` = now) |

```sh
curl -X POST 'http://127.0.0.1:8765/__mock/touch?id=1-200'     # 200 documents for the next delta sync
curl -X POST 'http://127.0.0.1:8765/__mock/fail?mode=503&path=/preview/&count=3'
curl -s http://127.0.0.1:8765/__mock/stats | python3 -m json.tool
```

## Fidelity notes

- Document list pages carry `all`, the ids of *every* match, as paperless-ngx's
  paginator does. At 100,000 documents that is ~690 KB per page before gzip.
  `--no-all-ids` drops it.
- Thumbnails are PNG (real: WebP). `/download/` honours `If-None-Match` too
  (real: only `/preview/`).
- One user is logged in (admin, superuser); object permissions are reported
  but nothing is hidden.
- Uploads are not stored: the new document gets the uploaded title and
  filename, a short marker text and a generated PDF.
- No WebSocket status channel, mail, workflows or OIDC.
- Full-text search has no stemming and no ranking.

## Decoding harness

`harness/` is a SwiftPM executable that drives the app's own `ApiRepository`
(packages `Networking`, `DataModel`, `Common` by relative path) against a
running mock and prints PASS/FAIL per step: login probe, version probe, every
element collection, paged lists, filters, search needles, every sort field,
`documentIDs`, `documents(ids:)`, `fetchContents(ids:)`, detail, metadata,
notes, suggestions, thumbnail, downloads (temporary file and content store
with ETag revalidation), mutations, upload and tasks, trash, the control
endpoints.

```sh
python3 scripts/mock_paperless/server.py --quiet &
cd scripts/mock_paperless/harness && swift run MockHarness        # [--url http://127.0.0.1:8765] [--token mock-token]
```

It needs at least 20,000 documents (needle ids). It puts back what it changes,
but the few documents it edits keep a new `modified`, and the ids of the
objects it creates are used up.

## Performance

Measured on an Apple-silicon Mac, Python 3.14, `--documents 100000`:

| | |
| --- | --- |
| start until the first API response | 1.3 s (25,000 documents: 0.5 s) |
| content index ready (built in the background; searches wait for it) | +1.0 s |
| RSS | ~90 MB at the first response, ~140 MB with the index, up to ~160 MB under parallel load (25,000: ~75 MB) |
| default list page, `page_size=250`, gzip | 15 ms; first visit of a page 60–110 ms (file checksums, sort); 380 KB on the wire, 950 KB of JSON |
| `fields=id` over the whole library (4 pages of 25,000) | ~100 ms (~60 ms without gzip) |
| `title_content` search, first page of 250 | 48 ms cold, 11 ms from the query cache |
| 200 sequential PDF downloads (8 MB) | 70–100 ms on one connection |
| 16 parallel connections, 2,400 mixed requests (444 MB) | 5.3 s, no errors |

Query results (the sorted id list) are cached until the next mutation, so
paging through a query filters and sorts once.
