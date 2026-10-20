# Widen the job search

## 1. Remote-first companies (Greenhouse / Lever / Ashby feeds) ✅
- [x] Shortlist remote-first companies that hire worldwide / APAC (probed ~65, kept 19)
- [x] Find each one's job board and confirm the feed works
- [x] Check what location text each feed uses → new per-company `"board": "global"` reuses the who-can-apply check instead of a regex
- [x] Add the working ones to `companies.list` (Board selector added to the Companies editor)
- [x] Test them and run a check → 2 QA roles found (Canonical Kernel Test Engineer, Binance QA Web3)
- [x] Read job descriptions from Greenhouse / Lever / Ashby so company jobs get fit scores too

## 1b. More Bangladeshi companies
- [x] Confirmed Bdjobs + LinkedIn already search every employer (Bdjobs just has few SQA posts: "SQA" = 3 results)
- [x] Probed ~110 BD employers on Greenhouse/Lever/Ashby/Workable/SmartRecruiters → none new (only Field Nation, already watched)
- [x] Probed ~45 BD careers pages → only Pridesys IT readable (added); the rest 404, JavaScript-only, or block crawlers
- [x] Other BD job sites: skill.jobs / NextJobz are JavaScript-only; Chakri, Mustakbil, Indeed BD, Jooble down or blocked
- [x] Looked up careers pages for big SQA employers (Enosis, SELISE, Therap, Tiger IT, Craftsmen, REVE, LeadSoft, Samsung R&D): none publish readable openings; they hire via LinkedIn / Bdjobs, which are already covered
- [x] Fixed Pridesys URL (www. returns a compressed page the reader can't decode)
- [x] LinkedIn: QA jobs sat behind 3 empty pages; now stops after `stop_after_empty_pages` (4) instead of 2
- [ ] Optional: reverse-engineer the skill.jobs / NextJobz job APIs

## 2. Search deeper on existing sources
- [ ] `global.himalayas.pages` 5 → 10
- [ ] `global.check_every_hours` 6 → 3
- [x] `linkedin.max_pages`: not needed: Bangladesh results run out at ~7 pages; the early stop was the real limit (fixed above)

## 3. Related job titles
- [ ] Add Software Engineer in Test, Test Developer, Quality Engineer, Automation Engineer to the filters and searches
- [ ] Decide on Support/Implementation Engineer and Technical Writer (different career direction)
- [ ] Add test titles to `test_filters.py`, run the tests

## 4. Relax own filters
- [ ] Remove "associate" from `filters.skip_level`?
- [ ] Keep Lead roles visible but ranked lower (already the case; confirm)

## 5. Restricted global jobs
- [ ] Leave `global.only_open_to_bd` on (decided: no country-restricted noise)

## Wrap-up
- [ ] Update README
- [ ] Run a full check and review the boards
