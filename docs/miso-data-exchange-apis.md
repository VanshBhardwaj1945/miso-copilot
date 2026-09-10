# MISO Data Exchange API reference

Every operation in MISO's two Data Exchange APIs, with which ones can be
sliced by region. Read from the portal's own published definitions on
2026-09-10: <https://data-exchange.misoenergy.org/>

Base URLs: `https://apim.misoenergy.org/lgi` and `https://apim.misoenergy.org/pricing`.
Every call needs a free Data Exchange subscription key; without one the API
returns `401 Access denied due to missing subscription key`.

> Everything here comes from the published definitions and their documented
> response examples, **not** from live calls - we have no subscription key yet.
> Confirm the two open questions at the end with your first real request.

## Which endpoints can be filtered by region

**12 of 32** - all of them in the Load, Generation and Interchange API.
None of the Pricing API can.

They all accept the same values:

```
region = NORTH | CENTRAL | SOUTH | MISO | NO_REGION
```

There is no default, so **omitting it returns every region** - and each row
carries its own `region`, so you can pull the whole set once and group it
yourself instead of making five calls.

| Endpoint | `region` in output | Also takes |
|---|---|---|
| `/v1/day-ahead/{date}/demand` | yes | - |
| `/v1/day-ahead/{date}/generation/cleared/physical` | yes | - |
| `/v1/day-ahead/{date}/generation/cleared/virtual` | yes | - |
| `/v1/day-ahead/{date}/generation/fuel-type` | yes | - |
| `/v1/day-ahead/{date}/generation/offered/ecomax` | yes | - |
| `/v1/day-ahead/{date}/generation/offered/ecomin` | yes | - |
| `/v1/day-ahead/{date}/interchange/net-scheduled` | yes | - |
| `/v1/forecast/{date}/load` | yes | `localResourceZone` |
| `/v1/forecast/{date}/outage` | yes | - |
| `/v1/real-time/{date}/demand/actual` | yes | `geoResolution`, `localResourceZone` |
| `/v1/real-time/{date}/generation/fuel-on-the-margin` | yes | - |
| `/v1/real-time/{date}/generation/fuel-type` | yes | - |

### The one that goes finer than region

`/v1/real-time/{date}/demand/actual` is the only operation with
**`geoResolution`** (`region` | `localResourceZone`, default `region`). That
changes the shape of the output rather than filtering it: rows come back with
`region` **or** `localResourceZone`, never both. Two calls if you want both.

`/v1/forecast/{date}/load` also takes `localResourceZone`, but as a filter.

## Which cannot

Pricing is geolocated by `node` (LMP) or `zone` (ASM reserve zones). A reserve
zone is not North/Central/South, so there is no way to ask for "day-ahead
prices in MISO South" - you pick nodes and group them yourself.

| Endpoint | API | Geography it does have |
|---|---|---|
| `/v1/historical/{date}/interchange/net-scheduled` | LGI | none |
| `/v1/real-time/{date}/binding-constraint` | LGI | none |
| `/v1/real-time/{date}/demand/forecast` | LGI | none |
| `/v1/real-time/{date}/demand/load-state-estimator` | LGI | `zone` |
| `/v1/real-time/{date}/generation/cleared/supply` | LGI | none |
| `/v1/real-time/{date}/generation/committed/ecomax` | LGI | none |
| `/v1/real-time/{date}/generation/offered/ecomax` | LGI | none |
| `/v1/real-time/{date}/interchange/net-actual` | LGI | none |
| `/v1/real-time/{date}/interchange/net-scheduled` | LGI | none |
| `/v1/real-time/{date}/outage` | LGI | none |
| `/v1/aggregated-pnode` | PRICING | `node` |
| `/v1/day-ahead/{date}/asm-exante` | PRICING | `zone` |
| `/v1/day-ahead/{date}/asm-expost` | PRICING | `zone` |
| `/v1/day-ahead/{date}/lmp-exante` | PRICING | `node` |
| `/v1/day-ahead/{date}/lmp-expost` | PRICING | `node` |
| `/v1/real-time/{date}/asm-exante` | PRICING | `zone` |
| `/v1/real-time/{date}/asm-expost` | PRICING | `zone` |
| `/v1/real-time/{date}/asm-summary` | PRICING | none |
| `/v1/real-time/{date}/lmp-exante` | PRICING | `node` |
| `/v1/real-time/{date}/lmp-expost` | PRICING | `node` |

Watch the near-misses: real-time `generation/fuel-type` has region, real-time
`generation/cleared/supply` does not.

## Load, Generation and Interchange API

`https://apim.misoenergy.org/lgi` - 22 operations.

### `GET /v1/day-ahead/{date}/demand`

**Day-Ahead Cleared Demand**

Demand cleared in the day-ahead market, by region in hourly or daily intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |
| `timeResolution` | string | hourly \| daily | hourly |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/day-ahead/{date}/generation/cleared/physical`

**Day-Ahead Cleared Generation, Physical**

Physical generation cleared in the day-ahead market, in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/day-ahead/{date}/generation/cleared/virtual`

**Day-Ahead Cleared Generation, Virtual**

Virtual generation cleared in the day-ahead market, in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/day-ahead/{date}/generation/fuel-type`

**Day-Ahead Generation Fuel Type**

Generation fuel mix (in megawatts) of units cleared in the day-ahead market for the various fuel types in the MISO footprint, by region in hourly intervals. Available at 2pm EST the day before. 'Coal' includes combination coal/gas units; 'Gas' includes combination oil/gas units; 'Storage' includes pumped storage, ESR, and battery units. 'Solar' data is only available from 2022-01-01 onwards.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/day-ahead/{date}/generation/offered/ecomax`

**Day-Ahead Offered Generation ECOMAX**

The Economic Maximum offered by generators in the day-ahead market, by region in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/day-ahead/{date}/generation/offered/ecomin`

**Day-Ahead Offered Generation ECOMIN**

The Economic Minimum offered by generators in the day-ahead market, by region in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/day-ahead/{date}/interchange/net-scheduled`

**Day-Ahead Net Scheduled Interchange**

Net scheduled interchange (NSI) that cleared the day-ahead market, by region in hourly intervals. Positive values indicate imports. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/forecast/{date}/load`

**Medium Term Load Forecast**

Medium term load forecast (in megawatts), by region and zone in hourly or daily intervals. Use the 'init' parameter to access a specific past run. Available at 7am EST the day after the init date.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `init` | string | - | - |
| `interval` | string | - | - |
| `localResourceZone` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |
| `timeResolution` | string | hourly \| daily | hourly |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/forecast/{date}/outage`

**Outage Forecast**

The total capacity (in megawatts at economic maximum) for all units that are expected to be on outage, by region in hourly intervals. This endpoint has no lookback, dates must always be current or future. Available at 6am EST up to 6 days before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/historical/{date}/interchange/net-scheduled`

**Historical Net Scheduled Interchange**

Net scheduled interchange (NSI) final volumes, by adjacent balancing authority (BA) in hourly intervals. Includes updates from Dynamic Schedules, Emergency Power Purchases, and ARS/CRSG events. Positive values indicate imports. Available at 7am EST the day after. May be updated up to 105 days after-the-fact.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `adjacentBa` | string | - | - |
| `interval` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/binding-constraint`

**Real-Time Binding Constraints**

The total number of binding constraints in the real-time market, in hourly intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/demand/actual`

**Actual Load**

Actual load (in megawatt-hours) as used in the real-time market, by region or grouped local resource zones (LRZ) in hourly or daily intervals. Available at 2am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `geoResolution` | string | region \| localResourceZone | region |
| `interval` | string | - | - |
| `localResourceZone` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |
| `timeResolution` | string | hourly \| daily | hourly |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/real-time/{date}/demand/forecast`

**Real-Time Cleared Demand**

Demand cleared (in megawatts) in the real-time market, in hourly or daily intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `timeResolution` | string | hourly \| daily | hourly |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/demand/load-state-estimator`

**Real-Time State Estimator Load**

Preliminary actual load (in megawatt-hours) as determined by the state estimator and reported in EIA-930 filings, in hourly or daily intervals. Available at 7am EST the day after. Aggregated by zone groups to preserve market participant confidentiality.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `timeResolution` | string | hourly \| daily | hourly |
| `zone` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/generation/cleared/supply`

**Real-Time Cleared Generation**

Generation cleared (in megawatts) in the real-time market, in hourly or 5-minute intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `timeResolution` | string | 5min \| hourly | hourly |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/generation/committed/ecomax`

**Real-Time Committed Generation, ECOMAX**

The Economic Maximum for all units committed in the real-time market and Forward Reliability Assessment & Commitment (FRAC) process, in hourly intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/generation/fuel-on-the-margin`

**Real-Time Generation Fuel-on-the-Margin**

Retrieve real-time generation fuel on the margin, by region in 5-minute intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `fuelType` | string | - | - |
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/real-time/{date}/generation/fuel-type`

**Real-Time Generation Fuel Type**

Generation fuel mix (in megawatts) as determined by the state estimator for the various fuel types in the MISO footprint, by region in hourly intervals. Available at 7am EST the day after. 'Coal' includes combination coal/gas units; 'Gas' includes combination oil/gas units; 'Storage' includes pumped storage, ESR, and battery units. 'Solar' data is only available from 2022-01-01 onwards.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `region` | string | NORTH \| CENTRAL \| SOUTH \| MISO \| NO_REGION | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`); rows carry `region`*

### `GET /v1/real-time/{date}/generation/offered/ecomax`

**Real-Time Offered Generation, ECOMAX**

The Economic Maximum from unit offers available to the real-time market and Forward Reliability Assessment & Commitment (FRAC) process, in hourly intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/interchange/net-actual`

**Real-Time Net Actual Interchange**

Net actual interchange (NAI), by adjacent balancing authority (BA) in hourly intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `adjacentBa` | string | - | - |
| `interval` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/interchange/net-scheduled`

**Real-Time Net Scheduled Interchange**

Net scheduled interchange (NSI) used in the Forward Reliability Assessment & Commitment (FRAC) process and Unit Dispatch System (UDS) cases, in hourly intervals. Positive values indicate imports. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/outage`

**Real-Time Outages**

The approved (planned) outages (in megawatt-hours) used in the real-time market and Forward Reliability Assessment & Commitment (FRAC) process, in hourly intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

## Pricing API

`https://apim.misoenergy.org/pricing` - 10 operations.

### `GET /v1/aggregated-pnode`

**Aggregated Pnode**

List of Aggregated Pnode with their types.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `date` | string | - | - |
| `node` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/day-ahead/{date}/asm-exante`

**Day-Ahead Ex-Ante MCP**

Historical ex-ante market clearing prices (MCP) for the day-ahead (DA) ancillary services market (ASM), by reserve zone in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `product` | string | Regulation \| Spin \| Supplemental \| STR \| Ramp-up \| R... | - |
| `zone` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/day-ahead/{date}/asm-expost`

**Day-Ahead Ex-Post MCP**

Historical ex-post market clearing prices (MCP) for the day-ahead (DA) ancillary services market (ASM), by reserve zone in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `product` | string | Regulation \| Spin \| Supplemental \| STR \| Ramp-up \| R... | - |
| `zone` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/day-ahead/{date}/lmp-exante`

**Day-Ahead Ex-Ante LMP**

Historical ex-ante locational marginal prices (LMP) for the day-ahead (DA) energy market, by commercial pricing node (CPNode) in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `node` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/day-ahead/{date}/lmp-expost`

**Day-Ahead Ex-Post LMP**

Historical ex-post locational marginal prices (LMP) for the day-ahead (DA) energy market, by commercial pricing node (CPNode) in hourly intervals. Available at 2pm EST the day before.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `node` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/asm-exante`

**Real-Time Ex-Ante MCP**

Historical ex-ante market clearing prices (MCP) for the real-time (RT) ancillary services market (ASM), by reserve zone in 5-minute intervals. Available at 7am EST the day after. [On May 6, 2026 Changes Applied including historical dates] 5-minute interval labels corrected. The database stores end-of-interval timestamps. Previously, the API returned interval values misaligned with database storage. Now corrected: 'value', 'start', and 'end' fields correctly represent period START time (and end time in 'end' field). To update stored data: subtract 5 minutes from all three fields. Before (old stored data): {"value": "2025-12-31T23:55:00", "start": "2025-12-31T23:55:00", "end": "2026-01-01T00:00:00"}. After (corrected): {"value": "2025-12-31T23:50:00", "start": "2025-12-31T23:50:00", "end": "2025-12-31T23:55:00"}. Apply this offset to all stored 5-minute interval records. No re-fetch required.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `product` | string | Regulation \| Spin \| Supplemental \| STR \| Ramp-up \| R... | - |
| `zone` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/asm-expost`

**Real-Time Ex-Post MCP**

Historical ex-post market clearing prices (MCP) for the real-time (RT) ancillary services market (ASM), by reserve zone in hourly or 5-minute intervals. Available at 7am EST the day after. It takes on average 3-5 days to finalize preliminary LMPs. [On May 6, 2026 Changes Applied including historical dates] Interval labels corrected. Database stores end-of-interval timestamps. Previously, API returned 5-minute values misaligned with storage. Now corrected: 'value', 'start', 'end' fields represent period START time. Hourly unchanged. To update stored 5-minute data: subtract 5 minutes from all three fields. Before (old): {"value": "2025-12-31T23:55:00", "start": "2025-12-31T23:55:00", "end": "2026-01-01T00:00:00"}. After (corrected): {"value": "2025-12-31T23:50:00", "start": "2025-12-31T23:50:00", "end": "2025-12-31T23:55:00"}. Apply offset to all stored 5-minute records. No re-fetch required.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `preliminaryFinal` | string | Preliminary \| Final | - |
| `product` |  | Regulation \| Spin \| Supplemental \| STR \| Ramp-up \| R... | - |
| `timeResolution` | string | 5min \| hourly | hourly |
| `zone` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/asm-summary`

**Real-Time MCP Summary**

Historical ex-post market clearing prices (MCP) for the real-time (RT) ancillary services market (ASM), at the MISO level in hourly intervals. Available at 7am EST the day after.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `product` |  | Regulation \| Spin \| Supplemental \| SER \| Mileage | - |
| `timeResolution` | string | hourly | hourly |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/lmp-exante`

**Real-Time Ex-Ante LMP**

Historical ex-ante locational marginal prices (LMP) for the real-time (RT) energy market, by commercial pricing node (CPNode) in 5-minute intervals. Available at 7am EST the day after. [On May 6, 2026 Changes Applied including historical dates] 5-minute interval labels corrected. The database stores end-of-interval timestamps. Previously, the API returned interval values misaligned with database storage. Now corrected: 'value', 'start', and 'end' fields correctly represent period START time (and end time in 'end' field). To update stored data: subtract 5 minutes from all three fields. Before (old stored data): {"value": "2025-12-31T23:55:00", "start": "2025-12-31T23:55:00", "end": "2026-01-01T00:00:00"}. After (corrected): {"value": "2025-12-31T23:50:00", "start": "2025-12-31T23:50:00", "end": "2025-12-31T23:55:00"}. Apply this offset to all stored 5-minute interval records. No re-fetch required.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `node` | string | - | - |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

### `GET /v1/real-time/{date}/lmp-expost`

**Real-Time Ex-Post LMP**

Historical ex-post locational marginal prices (LMP) for the real-time (RT) energy market, by commercial pricing node (CPNode) in hourly or 5-minute intervals. Available at 7am EST the day after. It takes on average 3-5 days to finalize preliminary LMPs. [On May 6, 2026 Changes Applied including historical dates - 5-MINUTE INTERVALS ONLY] Interval labels corrected. Database stores end-of-interval timestamps. Previously, API returned 5-minute values misaligned with storage. Now corrected: 'value', 'start', 'end' fields represent period START time. Hourly unchanged. To update stored 5-minute data: subtract 5 minutes from all three fields. Before (old): {"value": "2025-12-31T23:55:00", "start": "2025-12-31T23:55:00", "end": "2026-01-01T00:00:00"}. After (corrected): {"value": "2025-12-31T23:50:00", "start": "2025-12-31T23:50:00", "end": "2025-12-31T23:55:00"}. Apply offset to all stored 5-minute records. No re-fetch required.

| Parameter | Type | Values | Default |
|---|---|---|---|
| `interval` | string | - | - |
| `node` | string | - | - |
| `preliminaryFinal` | string | Preliminary \| Final | - |
| `timeResolution` | string | 5min \| hourly | hourly |

*paged (`pageNumber`, `pageSize`; loop until `lastPage`)*

## Two things to confirm with your first real call

1. **Does omitting `region` really return every region?** No default is
   documented, which conventionally means unfiltered - but the examples show
   one row each, so they do not prove it.
2. **Do `MISO` and `NO_REGION` rows come back too?** Both are in the enum. If a
   footprint-wide `MISO` row arrives alongside the three regions, summing
   everything double-counts.

Both resolve in a minute: call `/v1/real-time/{date}/generation/fuel-type` with
no `region` and look at the distinct values that come back.

