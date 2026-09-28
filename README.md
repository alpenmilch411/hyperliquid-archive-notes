# Notes on reading Hyperliquid's historical data

Hyperliquid publishes a complete per-minute history of its perp markets, free, going back to May 2023. It is better than what the paid vendors sell. CoinGlass keeps 1-minute data for 6 to 12 days. Coinalyze deletes intraday history daily. The archive keeps everything.

It is also easy to read wrong.

I have spent some time working with this data. Most of the mistakes I made had the same shape: the naive read succeeds, returns well-formed numbers, and is wrong. No exception. No missing column. No warning. Nothing to catch.

This repo is my attempt at cleaning that up. It is the list of what bit me, the evidence for each one, and a reader that handles them. Every claim here has a script in `probes/` that reproduces it. Run them. Do not take my word for any of it.

I am sure I have missed things, and I would rather hear about them than not. If you have hit something that is not on this list, or you think one of these is wrong, open an issue.

Two things to be honest about up front. Half the items below started as a number I had written down confidently and turned out to be wrong when I re-measured them for this repo. They are listed at the bottom. And the archive is still growing, so any absolute row count I print here is stale the day after I print it. The probes print today's number. That is the one to trust.

## Getting the data

Two sources, and they disagree in ways that matter.

**The info API** at `https://api.hyperliquid.xyz/info`. Free, no auth, POST JSON. This gives you the settled funding series (`fundingHistory`), the current universe (`meta`), and market snapshots.

**The archive**, a requester-pays S3 bucket:

```
s3://hyperliquid-archive/asset_ctxs/{YYYYMMDD}.csv.lz4
```

One lz4-compressed CSV per day, around 7 MB for a recent day. You pay the egress. The whole history is about a dollar. One day, to try it out, costs nothing worth counting:

```bash
aws s3 cp --request-payer requester \
  s3://hyperliquid-archive/asset_ctxs/20240315.csv.lz4 ./data/
```

The archive gives you per-minute open interest, premium, mark, oracle and mid prices, impact bid/ask and daily volume. The API gives you the settled funding rates. You need both, and the traps below are mostly about the seams between them.

## The traps

| # | What it is | Why you will not notice |
|---|---|---|
| 1 | `fundingHistory` is an **8-hour rate** before `2023-06-08T01:00:00Z`, hourly after | Same column, same dtype, no gap. Concatenate and you have an 8x error on the first 19 days |
| 2 | The archive's own `funding` column flips on a **different boundary**, 55 minutes earlier | Use one constant for both and you mislabel 55 minutes of rows |
| 3 | **The clamp bound changed.** ±0.0003 until `2023-12-11T22:00Z`, ±0.0005 after | The ±0.0005 in circulation is right about today and wrong about 2023 |
| 4 | Funding settles for the hour **ending** at H, published just after H | Assume the opposite and you have shifted the series forward by an hour |
| 5 | Hyperliquid writes **`0.0`, never NULL**, for absent data | There is not a single null in the archive. A dead book is a fabricated zero |
| 6 | **Negative open interest is real**, and the column is a coin quantity, not USD | A `> 0` sanity check never asks. And the biggest one is not the one it looks like |
| 7 | The API returns **`premium = NaN`** for the same condition the archive writes as `0.0` | Two encodings of "absent" across two sources, both silent |
| 8 | **Funding settlements can arrive late.** Worst case seen: 1,433 seconds | The obvious fix, widening your join tolerance, silently pulls in values that did not exist yet |
| 9 | **Three funding hours are missing** exchange-wide | "Every hour, always" is a reasonable assumption and it is false |
| 10 | **Delisted perps keep settling funding forever**, at exactly 0.0 | "Its history ended years ago" is the obvious premise. It is wrong for a quarter of the universe |
| 11 | The first archive day is **partial**, and an off-grid tick is an **extra row inside a minute** | `rows == coins * 1440` is the obvious integrity check. It fails on a third of days |
| 12 | `fundingHistory` **pages at 500 rows**, silently | A single call looks like it worked and gives you 500 hours instead of three years |
| 13 | `spotMeta`'s `index` is **not the array position** | `enumerate()` addresses the wrong market from position 71 |
| 14 | **There is no public liquidation feed** | And HyperEVM precompiles return *live* state at any block height, with no error |
| 15 | **Funding is censored.** About half of all rows carry no information | The clamp does not bind, the premium cancels, and funding sits exactly on the interest rate |
| 16 | A rolling z-score of funding is **`±inf` on real rows**, sign decided by rounding noise | Because of 15, the rolling standard deviation is exactly zero |
| 17 | Before a coin dies, **funding goes to zero and stays there for years** | The volume field freezes, so the row still looks liquid |
| 19 | A row stamped T is **one snapshot of the whole exchange, taken at T** to within about a second, every field at the same instant | Line it up against a live websocket and its values show up as late as 2.5 s after T. It looks like look-ahead. Most of it is the websocket's own delay |

### 1. The funding scale changed

`fundingHistory` returns an 8-hour rate before `2023-06-08T01:00:00Z` and an hourly rate from that timestamp on. Same field, same type, no discontinuity you can see.

If you concatenate the two, the first 19 days of the history are eight times too big.

You can prove which is which. Hyperliquid computes funding with this formula:

```
F_8h = premium + clamp(interest - premium, -C, +C),   interest = 0.0001
```

Run that formula on each row and compare it to the rate the API actually returned, dividing by 1 in one pass and by 8 in another. Before the boundary, all 1814 rows match when you divide by 1, and none of them match when you divide by 8. After the boundary, the opposite. These are not close matches, they are exact to about 19 decimal places.

`probes/funding_scale_flip.py`

### 2. The archive flips on a different boundary

The archive's own per-minute `funding` column also changes scale. It does it at `2023-06-08T00:05:00Z`, **55 minutes before** the settled series does.

Two columns, two different boundaries. If you use one constant for both, you mislabel 55 minutes of rows.

`probes/funding_scale_flip.py`

### 3. The clamp bound changed, and nobody says so

This is the one I would most want to be told.

The `C` in that formula is not a constant. It was 0.0003 until `2023-12-11T22:00Z` and 0.0005 from `23:00Z` on. The 0.0005 you will find quoted is right about today and wrong about the first half of the history.

To check it, only look at rows where the clamp is actually binding. On those rows the two candidate values predict different numbers, so a match cannot be luck. After the change, essentially every such row matches 0.0005 and none matches 0.0003. Before it, the reverse. Better than 99.4% either side.

One caveat, because I got this wrong the first time. The two are not perfectly separated. A couple of hundred rows across three years happen to match the *other* era's value by coincidence. So do not write a check that fails on a single disagreeing row. It will fail on real data. Look at a window of rows and take the majority.

`probes/clamp_bound_change.py`

### 4. Funding is published after the hour it describes

The rate stamped at hour H covers the hour *ending* at H, and it is published just after H. So it is available at H, not before. In the whole history there is not one settlement stamped earlier than the close of the hour it describes.

The median publication offset is 49 milliseconds. I had written "about 12 milliseconds" in my own notes, which is the 13th percentile, not the typical case.

The tail is what matters. There are five exchange-wide events where every coin settled late together, the worst at **+1,433 seconds**. Two of them are in 2025. There is a sixth at +25 seconds if you lower the cutoff, so state your threshold.

The tempting fix is to widen your join tolerance until the late rows land. Do not. A 24-minute tolerance means a row stamped at H can pick up a value that was not published until H plus 24 minutes. The rate itself is correct. It was published late. Null it and flag it.

`probes/when_funding_is_published.py`, `probes/late_settlements.py`

### 5. Zero is not null

There is not a single NULL in the archive. Not one, in 1,137 days across every column.

That is not because the data is complete. It is because Hyperliquid writes `0.0` for absent. This affects `mid_px`, `impact_bid_px`, `impact_ask_px`, `premium`, `open_interest` and `mark_px`.

A dead order book arrives as a book at zero. A range check accepts it happily.

`mark_px = 0` is the one that will hurt you: 3,759 rows across 28 days, an exchange-wide snapshot glitch, worst on 2023-07-12. A zero mark reaching any calculation is a fake -100% move.

`probes/zeros_not_nulls.py`

### 6. Negative open interest is real, and it is not in dollars

119 rows across 47 coins carry a negative `open_interest`. It is a settlement artifact around delistings. It is real data, and it passes a `> 0` check because nobody thinks to ask.

The part I got wrong: **the column is a coin quantity, not a USD notional.** I had "-1.37 billion" written down as if it were dollars. It is 1.37 billion BLAST, which at the prevailing oracle price is about **-$627,000**. The largest by actual notional is TON, at about **-$36 million**, which is 58 times bigger and was nowhere in my notes.

If you are going to quote a number from this column, multiply by the price first.

`probes/negative_open_interest.py`

### 7. The API says NaN where the archive says zero

The same absent value is encoded two different ways depending on where you read it.

The archive writes `premium = 0.0`. The API's `fundingHistory` returns `NaN`, on 4,427 settlements across 9 coins (STRAX, FRIEND, NFTI, OX, UNIBOT, SHIA, PANDORA, ZRO, RLB). Every one is a dead market with a funding rate of exactly 0.0.

There is a second trap sitting inside this one, and it cost me an hour. The API returns every number as a **string**, including this one:

```json
{"coin":"STRAX","fundingRate":"0.0","premium":"NaN","time":1710471600208}
```

So if you check for a bad value with `isinstance(x, float)`, it never fires, because at that point it is still `"NaN"`, the string. You only see it after you convert, which every real caller has to do. I convinced myself this whole trap did not exist because of that, and nearly deleted a true finding from this list.

`fundingRate` never comes back as NaN. Only `premium` does.

And the obvious fix is a trap of its own: reject non-finite premium outright and you delete nine real coins from your universe.

`probes/premium_nan_in_api.py`

### 8, 9. Missing hours

Three funding hours are missing exchange-wide: `2023-07-02T20`, `2023-08-23T20`, `2024-08-15T13`. Each one is a clean 100% miss of every coin active at the time.

If you assert a dense hourly grid, this raises on real data. It is not your bug.

`probes/missing_funding_hours.py`

### 10. Delisted perps never stop settling

55 of the 232 perps in the archive are delisted. Every one of them is still emitting hourly funding settlements right now, at exactly 0.0, and has been since it died.

"A delisted coin's history ends when it was delisted" is the obvious premise. It is false for a quarter of the universe, and believing it will disable any staleness check you build.

`probes/delisted_still_settle.py`

### 11. The first day is partial and minutes can hold extra rows

The archive's first day, 2023-05-20, has 1,270 minutes, not 1,440.

And an off-grid tick is an **extra row inside a minute**, not a replacement for it. ARB on 2023-05-22 has 1,445 rows across 1,440 distinct minutes.

So `rows == coins * 1440` fails on a third of all days. Count coverage in distinct minutes.

`probes/partial_first_day.py`

### 12. Pagination is silent

`fundingHistory` returns at most 500 rows. Ask for three years of BTC and you get a `200 OK`, a well-formed JSON array, and 500 rows, stopping 1,074 days short of where you asked. There is no error, no flag, no cursor, nothing to tell you it truncated.

Page it. And note that `startTime` is **inclusive**, so advancing the cursor to the last row's timestamp will duplicate that row. Advance to `last + 1`.

`probes/funding_history_pagination.py`

### 13. `spotMeta`'s index is not a position

310 spot pairs, maximum index 700, 391 gaps. `enumerate(universe)` diverges from the real index at position 71 and silently addresses the wrong market from there on.

`probes/spot_meta_index.py`

### 14. No liquidations, and the precompiles lie about history

There is no public liquidation feed. `liquidations`, `liquidationHistory` and `allLiquidations` all return 422. Liquidations only appear inside per-user event streams, which need an address.

Worse, and this one is genuinely nasty: HyperEVM's read precompiles ignore the block height you ask for. I called the oracle-price precompile at five heights, from `latest` down to a block more than a year old, and got the identical current value every time. No error. Your historical query succeeds and hands you today's price.

`probes/no_liquidation_feed.py`, `probes/precompiles_ignore_block_height.py`

### 15. Funding is censored

This is the one that changed how I look at the whole dataset.

Go back to the formula:

```
F_8h = premium + clamp(interest - premium, -C, +C)
```

When the clamp does not bind, the middle term is just `interest - premium`, so the whole thing is `premium + interest - premium`. **The premium cancels out.** What is left is the interest rate, and nothing else. On those rows funding tells you nothing about the market. It is a constant.

This is not rare. About **half** of all settled rows sit exactly on `1.25e-5`, which is the interest rate divided by eight. Another 11% are exactly zero. That leaves about 38% of rows where funding is actually following the market.

You can check that the formula is the reason, rather than a coincidence. Take only the rows where the clamp *does* bind. Almost none of them land on `1.25e-5` by chance, about one in a million. So the big pile of rows sitting exactly on that value is not a quirk of the data. It is just the rows where the clamp did not bind, and on those rows the formula gives you the interest rate and nothing else.

Half of this column is a constant. Whatever you compute from it, you are computing from that.

`probes/funding_is_censored.py`

### 16. A rolling z-score of funding blows up

This falls straight out of 15, but it is quiet enough to deserve its own entry.

Because half the rows are that same constant, a coin can sit on it for a whole 30-day window without moving. The rolling standard deviation over that window is then exactly zero. A z-score divides by it, and you get infinity.

It is not rare and it is not only dead coins. Around twelve thousand rows across thirty-odd coins, from ones doing a few hundred dollars a day up to fifty million.

The sign is the bad part. The top of the fraction on those rows is not a real difference. It is the smallest number a float can represent next to the constant, left over from rounding inside the rolling sum. So whether a row comes out `+inf` or `-inf` is decided by floating point noise. It came out roughly 48/52 in my data.

Sort by the absolute z-score and these rows come out on top, every time, with a sign that means nothing.

Put a floor under the standard deviation, and get the floor from the data rather than picking one.

`probes/zero_variance_zscore.py`

### 17. Death looks like liquidity

Before a coin is delisted, Hyperliquid stops publishing a rate for it. Funding goes to exactly `0.0` and stays there.

Three things about that which took me a long time to get right, and which I had written down wrong.

**It is not a 24-hour event.** STRAX's zero-block runs for 850 days and is still open at the data cutoff. FRIEND 802. FTM 547. JELLY 473. These are not brief pre-delisting windows. They are the rest of the coin's recorded life.

**The book is already gone.** Funding zeroes one or two settlements *after* the order book stops publishing, not before. It is a symptom, not a warning.

**And the row still looks liquid,** which is the actual trap. `day_ntl_vlm` does not go to zero. It stops updating and holds its last value. STRAX sits at exactly `784,843.9669` for weeks after its book is gone. So if you filter on daily volume to drop dead markets, these rows survive the filter.

Why this matters: a drop from a large funding rate to exactly zero looks, to anything that measures change, like a huge real move. It is not a move. The exchange switched the coin off.

`probes/delisting_zero_funding.py`

### 18. One thing I cannot explain

Three coins do not get a funding settlement for the hour they were listed in. RUNE (first seen at 12:59), ZRO (01:55) and MEME (13:59) each get their first settlement an hour later than the rule would suggest.

The obvious explanation is that a coin listed near the end of an hour is not settled for that hour. That explanation is wrong. CELO, HBAR and BSV were all first seen at minute :58, later than ZRO, and they settle normally. And HMSTR, listed exactly on the hour, settles an hour *earlier* than other coins listed exactly on the hour.

Three coins out of 205 genuine listings. I have not found a rule in the public data that predicts them. If you know it, tell me.

`probes/late_listing_skip.py`

### 19. What the time on a row means

Someone asked me this, and I did not know. A row stamped `14:57:00`: is it the market at 14:57:00, the minute averaged, or whenever the recorder got round to it? Can anything in it come from after 14:57:00? And are open interest and the mark price in the same row from the same moment?

Short answer: it is a single snapshot, taken at T to within about a second, and every field in it is from the same instant. It is not an average and not a loose recording time.

To check, I recorded Hyperliquid's public websocket from a server in Frankfurt and lined the archive up against it. Two feeds: `activeAssetCtx` for BTC, ETH and PAXG, which pushes the same ten fields about once a second, and `allDexsAssetCtxs`, which pushes the whole universe about once every 15 seconds. The overlap with the archive is 586 minutes on 2026-09-26 and 2026-09-27. That is 1,758 rows at one-second resolution and 137,124 rows across all 234 main-dex coins at 15-second resolution.

**It is one snapshot, not an aggregate.** For 87% of the BTC, ETH and PAXG rows, a single websocket push matches the row on all ten fields, exactly. An average would not do that. For the rest, the moment fell between two pushes.

**It is taken at a fixed point, not whenever.** On my clock, a snapshot at T+0.4 s fits 98.4% of minutes, all three coins at once (550 minutes where all three could be placed). At T+1.5 s only 47% fit. At T+3 s, 8.5%. At T−1 s, 7.8%. Whatever writes the archive takes its picture at the same point in every minute. Across the whole universe the same offset holds, at the coarser resolution: T+0.7 s fits 95.6% of rows.

**Every field is from the same instant.** Of the 1,729 rows where every field could be placed, one single moment fits all ten fields in 99.5%. The 9 rows where none does are spread over six different fields, open interest the most with 4. No field leads or lags the others. It also holds across coins: in 99.3% of minutes, one moment fits BTC, ETH and PAXG together. So it looks like one snapshot of the whole exchange per minute. Joining open interest to mark or oracle price from the same row is safe.

**Can a value come from after T?** On my clock, yes. In 18% of rows the snapshot has to be after T, and single values first appeared on my feed up to 2.55 seconds after T.

But my clock is not the exchange's. The websocket delivers late. `trades` frames carry the exchange's time, and they arrive 295 / 361 / 1,001 ms after it (1st, 50th, 99th percentile). The context frames carry no exchange time at all. Lining their mid price up against the `bbo` feed puts them at roughly 0.5 to 0.7 seconds late. That is a rougher estimate, so I would not lean on it.

Take the median `trades` delay off and only 1.3% of rows are still provably after T. Take the 99th percentile off and it is 2 rows out of 1,720. The largest gap I can prove on my own clock is 1.04 seconds, before taking any delay off.

So I think the snapshot is the exchange's state at T, give or take about half a second, and I cannot tell you which side of T it falls on. Any look-ahead is bounded, and it is about a second at most. If you need a hard guarantee, treat a row stamped T as known at T+3 s. That covers every value in my one-second sample, even on my late clock.

A worked example. BTC, `2026-09-26T01:00:00Z`. The row has mark `83868`, oracle `83909.5`, funding `0.0000051465`, day volume `2876124270.08946`. The push that reached me at T+0.29 s had the row's day volume but the old prices, mark `83846.4` and oracle `83904.0`. The next push arrived at T+2.55 s, because one was missing in between. It had the row's prices, but the volume had already moved on. So the row is the state at one moment between those two pushes. "First seen at T+2.55 s" is where the prices showed up on my feed, not when the snapshot was taken.

**Compare numbers, not text.** The two sources print the same number differently. The archive writes `84581` and `0`, the websocket writes `84581.0` and `0.0`. For `prev_day_px`, only 57% of matches are the same text. Parse both with `Decimal`, not `float`.

Open interest and day volume are harder. They are running sums, and both sources print them with float noise, but not always the same noise. The websocket says `1106151.6259999992` where the archive says `1106151.626`. Of the matches, 4% for open interest and 41% for day volume only match within float noise, the largest gap 3.3e-14 of the value. An exact join on either of those two columns will quietly drop rows.

Honest caveats. Every offset here is against the time frames reached my machine, not the exchange's time. That machine's clock was checked against NTP every five minutes and was within 30 ms, usually 5. The one-second timing only covers BTC, ETH and PAXG, and the universe-wide check only resolves to about 15 seconds. The archive has no HIP-3 rows, so this is main dex only. And it is only 586 minutes over two days: the bucket's files for 2026-09-26 and 2026-09-27 stop at 06:52 and 02:55, so those days are partial. If the writer changes, this changes with it.

`probes/row_timestamp_semantics.py`

## The reader

`hl_archive.py` is my attempt at a clean read. One file, no package, nothing clever. It does the four things I found a correct read needs:

- parses the lz4 CSV with proper dtypes and UTC timestamps
- turns Hyperliquid's `0.0`-for-absent into real nulls, with a `*_missing` flag beside each one so nothing disappears quietly (including the `open_interest <= 0` case, which catches the negatives)
- labels which funding scale each row is on, so you cannot concatenate an 8-hour rate with an hourly one by accident
- refuses the corruption that would otherwise pass, and counts coverage in distinct minutes rather than rows

```python
import hl_archive

df = hl_archive.read_day("data/20240315.csv.lz4")
```

It is deliberately small and it stops at the frame. It does not know or care what you do next.

It handles what I found. It almost certainly does not handle everything. If it drops something on the floor for you, I want to know.

## Running the probes

```bash
git clone https://github.com/alpenmilch411/hyperliquid-archive-notes
cd hyperliquid-archive-notes
uv sync            # or: pip install polars lz4 requests
```

The API probes need nothing but a network connection:

```bash
python probes/funding_history_pagination.py
python probes/spot_meta_index.py
python probes/delisted_still_settle.py
python probes/precompiles_ignore_block_height.py
```

The archive probes need archive days on disk. Point them at wherever you put them:

```bash
python probes/funding_scale_flip.py     --archive-dir ./data
python probes/clamp_bound_change.py     --archive-dir ./data
python probes/zeros_not_nulls.py        --archive-dir ./data
python probes/negative_open_interest.py --archive-dir ./data
```

One probe needs a recording of the public websocket as well as the archive. Record, wait for that day to land in the bucket (it appears the next morning, UTC), download it, then compare:

```bash
python probes/row_timestamp_semantics.py record  --minutes 120 --out rec.jsonl
python probes/row_timestamp_semantics.py compare --archive-dir ./data --recording rec.jsonl
```

Every probe prints the number it measures, today, along with the population it measured over. That is on purpose. The archive is still growing, so any figure I hardcode into this README will drift. The probes will not.

## What I got wrong

I re-measured every claim here before publishing, and a lot of them did not survive. I am listing them because the whole point of this repo is that confident numbers about this dataset are usually wrong, and mine were no exception.

- **"-1.37 billion dollars of negative open interest."** It is 1.37 billion BLAST, which is about -$627,000. The real headline is TON at -$36 million.
- **"Settlement offsets are about 12 milliseconds."** The median is 49 milliseconds. 12 ms is the 13th percentile.
- **"The premium NaN doesn't exist."** It does, in the API, on 4,427 rows. I had checked the wrong source and nearly deleted a true finding.
- **"The zero-funding blocks last about 24 hours."** They last years.
- **"Those rows are still liquid."** The volume field is frozen. They are not.
- **"The separation between the two clamp eras is total."** It is 99.4%, not 100%, and a hard assertion built on "total" fires on real data.
- **"Coins listed late in an hour skip that hour's settlement."** Three coins do. The rule does not hold for coins listed later in the hour than they were.
- **"rows == coins * 1440 fails on 28% of days."** 33%.

If you find another, open an issue. I would rather be corrected than quoted.

## License

MIT.
