# Crop-Condition Forecast — Design (DRAFT, needs refinement)

> Status: **draft** — thresholds and a few decisions still need the owner's input
> (see "Open decisions"). No code written yet.

## Goal

Given a weather forecast for the next *N* days, predict the **condition of the
crop** (e.g. "heat spike then heavy rain") and **how it impacts the specific
crop** (e.g. pomegranate → sunburn, then fruit-cracking + bacterial-blight
pressure), with concrete recommended actions.

This is the first feature that makes **PA's weather model and the external
farming app work together** instead of in isolation.

## Why compose the two systems

| | PA (`personal_assistant`) | External app (`projects/farming`) |
|---|---|---|
| Weather | `weather.forecast(days=7)` — rich: rain mm, rain probability, Tmax/Tmin, **ET₀**, wind, soil moisture & temp | only 3-day, coarse (precip_sum + Tmax/Tmin) |
| Crop brain | none | llama3 + crop KB (`core/inference.py`) |
| Crop KB | — | `crops/pomegranate.json` — **disease-only**, no climate-stress thresholds, no phenology |

PA already has the better forecaster; the app has the agronomic reasoning but a
weak weather feed and no climate-impact model. Nobody bridges them today.

**Principle preserved:** farming questions still go to the external app first.
PA becomes the *weather-data provider*; the app stays the *brain*.

## Architecture — 3-stage pipeline

```
"crop condition next 4 days?"
        │
   PA detects intent ──► PA weather model: forecast(days=4)   [rich: Tmax, rain, ET0, soil]
        │                          │
        └──────────── POST forecast + crop ──────────────►  EXTERNAL FARMING APP
                                                                   │
                                          (1) deterministic crop-stress analyzer
                                              (per-day flags from pomegranate thresholds)
                                                                   │
                                          (2) llama3 + crop KB writes narrative & actions
                                                                   │
                          ◄──── { per_day:[{date,flags,severity}], summary, actions } ────
```

**Why a deterministic layer, not pure-LLM:** "42°C causes pomegranate sunburn",
"sudden rain after a dry spell cracks fruit" are *quantitative agronomic facts*.
Handing raw numbers to the LLM and asking "is this bad?" gives inconsistent
answers and invented thresholds — exactly what the eval suite penalises. So we
**compute stress flags from thresholds** (reliable, explainable, auditable) and
let the LLM only **explain and recommend**. Numbers stay trustworthy; prose
stays readable. (Chosen approach: deterministic flags + LLM narrative.)

## A. Crop KB extension — `crops/pomegranate.json`

Add a `climate_stress` block beside the existing `diseases`. Stage-aware,
because the same weather hurts differently at different growth stages.

```jsonc
"climate_stress": {
  "stages": ["vegetative", "flowering", "fruit_set", "fruit_development", "maturation"],
  "thresholds": {
    "heat":     { "tmax_warn_c": 40, "tmax_severe_c": 42,
                  "worst_stages": ["fruit_development", "maturation"] },   // sunburn, cracking
    "cold":     { "tmin_warn_c": 8,  "tmin_severe_c": 5 },                 // flower/young-fruit damage
    "rain":     { "warn_mm": 25, "severe_mm": 40,
                  "worst_stages": ["flowering", "maturation"] },           // flower drop / cracking + blight
    "humidity": { "rh_warn_pct": 80, "rh_severe_pct": 88 },                // bacterial blight, anthracnose
    "wind":     { "warn_kmh": 40, "severe_kmh": 55 },                      // fruit/branch damage
    "water":    { "et0_high_mm": 6.5, "soil_moist_low": 0.15 }             // water stress -> size loss, cracking
  },
  "compound_rules": [
    { "id": "dry_then_wet_cracking",
      "when": "water_stress_day followed within 2d by rain>=warn_mm",
      "stages": ["fruit_development","maturation"], "severity": "high" }
  ]
}
```

> ⚠️ Starting values from general Bhagwa/Maharashtra pomegranate agronomy —
> **need validation/tuning** (Open decision #1).

## B. Deterministic analyzer — `crop_stress.py` (external app)

Pure function, no LLM. Each forecast day → zero or more flags:

```jsonc
classify(forecast_days, thresholds, stage) -> [
  { "date": "2026-06-25",
    "flags": [ { "type": "HEAT_STRESS", "severity": "high",
                 "evidence": "Tmax 43C >= 42C severe limit", "metric": {"tmax_c": 43} } ],
    "day_severity": "high" }, ...
]
```

- Flag taxonomy: `HEAT_STRESS, COLD_STRESS, HEAVY_RAIN, HIGH_HUMIDITY, WIND_DAMAGE, WATER_STRESS, CRACKING_RISK` (compound).
- Severity scale: `none | watch | warn | high`.
- Every flag carries `evidence` (rule + number) so the output is auditable and the LLM cannot fudge it.

## C. Endpoint contract — `POST /api/crop-forecast` (external app)

```jsonc
// request (PA sends its rich forecast)
{ "crop": "pomegranate", "location": "Barloni", "stage": "fruit_development",
  "forecast": [ {"date":"...","temp_max_c":43,"temp_min_c":27,"rain_mm":2,
                 "rain_probability_pct":10,"evapotranspiration_mm":7.1,
                 "wind_max_kmh":22,"soil_moisture_now":0.12}, ... ] }

// response
{ "per_day": [ {"date":"...","flags":[...],"day_severity":"high"}, ... ],
  "overall_severity": "high",
  "summary": "Days 3-4 bring a 43C heat spike then 30mm rain - sunburn risk then fruit-cracking and bacterial-blight pressure.",
  "actions": ["Run mulch/shade-net before Day 3",
              "Hold copper spray until rain clears (washes off <4h)",
              "Light evening irrigation to buffer heat, stop 24h before Day-4 rain to avoid cracking"],
  "model": "llama3:latest", "method": "deterministic_flags+llm_narrative" }
```

llama3 receives **flags + crop KB** (not raw numbers to judge) and writes only
`summary` + `actions`.

## D. PA side

1. **Intent** — keywords like `crop condition`, `impact`, `affect`, `next N days`
   with a crop/weather context. Not local-only, so it already routes to the app.
   PA adds a thin branch in `handle()` / `farming_client` that takes the enrich path.
2. **Client** — `farming_client.crop_forecast(days, crop, location)`: calls PA's
   `weather.forecast(days)` then `POST /api/crop-forecast`. Falls back to PA's own
   `qwen3` narrative if the app is down.

**Net:** PA forecasts weather → app forecasts crop. Clean split, no duplication.

## Open decisions (need refinement)

1. **Thresholds** — accept the starting values above, or set preferred numbers for
   Bhagwa pomegranate (esp. heat-severe and cracking-rain limits)?
2. **Growth stage** — where does `stage` come from?
   (a) derive from the crop record's plant/flowering date in the farming DB,
   (b) infer from month/bahar calendar, or (c) ask the user. Big accuracy impact.
3. **Forecast horizon** — fix at 4 days, or parameterise up to 7 (Open-Meteo free daily limit)?
4. **Multi-crop** — pomegranate only for now, or pre-shape the KB schema so
   sugarcane/banana slot in later? (Schema already generalises.)
5. **Where the LLM runs** — external app's llama3 (default, app is the brain) or
   allow PA's qwen3 as the narrator when the app is down (fallback only)?
