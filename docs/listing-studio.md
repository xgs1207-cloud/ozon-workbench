# Desktop listing studio

Flow: choose captured specifications → confirm factual summary → confirm a real
Ozon category (keywords optional) → select/confirm one AI copy candidate → edit
references/prompts and generate images → fill the card → review and publish.

## Keyword and risk boundaries

A research category or keyword-library query is not required after the operator
chooses the official Ozon category/type. Empty keyword arrays mean fact-led copy,
not invented keyword demand. Existing chosen keywords keep relevance/claim checks.
AI tags enter the official card only from the current confirmed copy; stale copy
cannot supply tags.

Missing optional facts do not prohibit preparing a truthful draft. Logistics and
required attributes are checked when compiling the card. An unresolved compliance
warning still prevents official submission/export. Known unsafe/illegal/identity
conflicts or an explicit AI rejection remain preparation blockers. Original risk
evidence is retained, not overwritten to make the workflow look successful.

## Image prompts

The media step shows the entire real input reference list even before a paid AI
plan is created, an editable prompt for
each planned slot, and a results gallery. Select 1–3 references per slot for the
current image backend; exceeding the limit never silently discards selections.
Save the slot and approve the plan before generating one explicitly requested
image. A paid request is not retried automatically and generated images still
need review. Reading/saving/loading/deleting prompt-library entries never calls
an AI model. Prompts are stored in the existing SQLite database; saving the same
name updates that template, not any already-saved product slot.

## User-requested card defaults

Defaults have independent `user_requested_default` provenance. They are not
supplier-confirmed facts. Official dictionary values must be resolved for the
actual shop/category; numeric IDs are never guessed. Manual changes and explicit
clears win, captured conflicts remain visible, and official required fields are
never suppressed merely because the requested default is blank.

| Field | Policy |
| --- | --- |
| Brand | Official no-brand option; hidden only when safely resolved |
| Merge model name | Stable random WB identifier, shared across the product's variants |
| Origin | Official China option |
| 18+ | False |
| Original factory package count | 1 |
| Statistical quantity, marking code, warranty, optional seller code, similar-product grouping, EAEU HS code, shelf life | Blank; advanced optional UI |
| Tags | Current confirmed AI copy |

The optional seller-code characteristic is **not** the required unique `offer_id`.

## Original 1688 videos and COS

Captured source videos are first privately saved and inspected. The operator
selects videos, confirms rights and variant fit, then explicitly publishes them
to configured Tencent COS. No user-provided external URL or guessed publication
proof can replace the original file. All files/SKU bindings/technical parameters
are checked before storage calls. Content-addressed objects require SHA, byte
size, content type and MD5/ETag checks on authenticated COS HEAD and anonymous
HEAD. Matching objects can be reused without another PUT. Partial failures retain
successful ledger entries and preserve the prior listing selection.

The ordinary-video section of [Ozon's import method](https://docs.ozon.ru/api/seller/#operation/ProductAPI_ImportProductsV3)
in the official 2026-10-03 snapshot describes MP4/MOV URLs, complex group 100001,
URL attribute 21841 and title attribute 21837, without a domain whitelist.
Current live documentation could not be fully re-fetched due to anti-bot/access
errors. The implementation accepts only its own verified stable COS URLs (plus
legacy supported platform links), not arbitrary `.mp4` addresses. Actual Ozon
import acceptance/moderation is **not live verified**; first authorized import
must be followed by readback. This does not add short-video-cover support.

No development test makes a paid model call, publishes real COS objects, or writes
to a real Ozon store; these boundaries are mocked in isolated tests.
