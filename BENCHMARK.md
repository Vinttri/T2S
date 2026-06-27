# 🔬 T2S — Questions, gold SQL & expected values

**Verify T2S yourself.** Ask each question below (in the UI, or head-less with the `curl` shown), then compare the result to the **expected value**. The **gold SQL** is included so you can see exactly how each expected value is derived and re-run it directly on the database.

> Values were produced by executing the gold SQL on the shipped databases. Config for the T2S runs in [`tests.md`](tests.md): completion `gemma-4-12b-it-qat`, temperature 0. A few *analytical* gold queries intentionally deviate from the loaded business knowledge (noted in `tests.md`); there the gold value is what the gold SQL returns, which T2S may answer differently by design.

```bash
# ask T2S head-less (swap the graph + question)
curl -s -X POST http://localhost:5050/graphs/<DB>/sql \
  -H 'Content-Type: application/json' \
  -d '{"question":"<paste a question>","use_knowledge":true,"use_user_rules":true}'
```

Databases: **`cybermarket_pattern_large`** (online-marketplace, non-training schema) · **`sports_events_large`** (Formula-1).

---

## 1. cybermarket_pattern_large  ·  graph `cybermarket_pattern_large`

_Online-marketplace / risk domain — a schema T2S was **not** tuned on._

**1.** How many platforms show as 'active' right now? · _Simple_

Expected → **`244`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) AS active_platforms
FROM markets m
WHERE m."OperStatus" = 'Active';
```
</details>

**2.** How many shoppers are using advanced authentication? · _Simple_

Expected → **`329`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) AS adv_auth_buyers
FROM buyers b
WHERE b."AuthLevel" = 'Advanced';
```
</details>

**3.** What's the overall revenue from digital goods? Round the result to 2 decimal places. · _Simple_

Expected → **`1983202.31`**

<details><summary>gold SQL</summary>

```sql
SELECT ROUND(SUM((tp."PriceAmt")*tp."QtySold")::numeric,2) AS total_digital_sales_value
FROM transaction_products tp
WHERE tp."ProdCat" = 'Digital';
```
</details>

**4.** Break down transactions by how complicated their shipping routes were, then show me the counts with the trickiest routes at the top. · _Simple_

Expected → **`Complex|346 ; Simple|340 ; Medium|314`**

<details><summary>gold SQL</summary>

```sql
SELECT t."RouteComplex" AS route_complexity,
       COUNT(*) AS txn_count
FROM transactions t
GROUP BY route_complexity
ORDER BY txn_count DESC;
```
</details>

**5.** Tell me how the average security score stacks up across sessions with different privacy levels, rounded to 2 decimal places, from totally open to fully masked connections. · _Simple_

Expected → **`3.6515646971641305|99.90 ; 5.965116073773755|99.80 ; 9.152745074132744|99.70 ; 8.698129018152603|99.70 ; 4.791067603169464|99.30 …(+995 rows)`**

<details><summary>gold SQL</summary>

```sql
SELECT c."AnonLevel" AS anonymity_level,
       ROUND(AVG(c."OpSecMetric")::numeric,2) AS avg_opsec
FROM connection_security c
GROUP BY anonymity_level
ORDER BY avg_opsec DESC;
```
</details>

**6.** Give me all platforms sorted by its risk score, most dangerous on top and show 4 digits. · _Simple_

Expected → **`MK8205|0.4222 ; MK6895|0.4118 ; MK9678|0.4105 ; MK5893|0.4103 ; MK5750|0.4085 …(+949 rows)`**

<details><summary>gold SQL</summary>

```sql
SELECT m."PlatCode",
       ROUND(
         (0.4*(m.platform_compliance->>'vuln_inst_count')::numeric +
          0.3*(m.platform_compliance->>'sec_event_count')::numeric +
          0.3*COALESCE(NULLIF(REGEXP_REPLACE(m."RepScore",'[^0-9.]','','g'), '')::numeric,0))/100
       ,4) AS mrs
FROM markets m
ORDER BY mrs DESC;
```
</details>

**7.** I need the total number of transactions that were both marked as fraud and involved cross-border payments. · _Simple_

Expected → **`154`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) AS cross_border_fraud
FROM transactions t
JOIN risk_analytics ra ON ra."TxnLink" = t."EventCode"
WHERE ra."FraudProb" > 70
  AND t."CrossBorder" = 1;
```
</details>

**8.** Calculate how many hours we typically take to close Tier-3 escalations. Show the average value, rounded to hundredths. · _Simple_

Expected → **`86.14`**

<details><summary>gold SQL</summary>

```sql
SELECT ROUND(AVG( (a.alert_case_management->>'resolve_hours')::numeric ),2) AS avg_resolve_hrs_t3
FROM alerts a
WHERE a.alert_case_management->>'escalation_tier_stat' = 'Level3';
```
</details>

**9.** How many critical alerts do we have? · _Simple_

Expected → **`256`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) AS critical_alerts
FROM alerts a
WHERE a.alert_case_management->>'severity_level_stat' = 'Critical';
```
</details>

**10.** What's the ratio of sales went through escrow? Round to 2 decimal places. · _Simple_

Expected → **`0.28`**

<details><summary>gold SQL</summary>

```sql
SELECT ROUND(
  SUM( CASE WHEN (t.transaction_financials->>'escrow_used_stat') = 'Yes' THEN 1 ELSE 0 END)::numeric /
  NULLIF(COUNT(*),0),2) AS escrow_use_ratio
FROM transactions t;
```
</details>

**11.** How many message threads contain irregular phrasing, sudden language switches, or machine translated text that indicate possible deception? · _Simple_

Expected → **`319`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) AS suspicious_language_threads
FROM communications c
WHERE c.communication_details->>'lang_pattern_type' = 'Suspicious';
```
</details>

**12.** How many buyers have unpredictable spending trends? · _Simple_

Expected → **`246`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) AS variable_spend_buyers
FROM buyers b
WHERE b.buyer_risk_profile->>'spend_pattern' = 'Variable';
```
</details>

**13.** I want to know the keyword-hitting values for all customer and internal chats to identify high-risk patterns. Round to 3 decimal places and show in descending order · _Simple_

Expected → **`TX1124392|1.667 ; TX4786463|1.417 ; TX3158939|1.300 ; TX7993013|1.267 ; TX7554480|1.154 …(+995 rows)`**

<details><summary>gold SQL</summary>

```sql
SELECT cm."EventLink",
       ROUND(
         (cm.communication_details->>'keyword_match_count')::numeric /
         GREATEST((cm.communication_details->>'msg_count_total')::numeric,1)
       ,3) AS ssd
FROM communications cm
ORDER BY ssd DESC;
```
</details>

**14.** Give me how fast each session processed threats, and the levels of login verification for buyers. · _Moderate_

Expected → **`TX1804921|20.0|Unknown ; TX7643969|20.0|Unknown ; TX1492864|19.94|Basic ; TX8778100|19.91|Unknown ; TX2173190|19.91|Basic …(+995 rows)`**

<details><summary>gold SQL</summary>

```sql
SELECT c."TxnPointer" AS session_id, SPLIT_PART(c."Threat_handle_rate", ' ', 1)::numeric AS thr, COALESCE(b."AuthLevel", 'Unknown') AS buyer_auth_level FROM connection_security c LEFT JOIN transactions t ON t."EventCode" = c."TxnPointer" LEFT JOIN buyers b ON b."AcqCode" = t."AcqLink" ORDER BY thr DESC NULLS LAST;
```
</details>

**15.** What's the average distance traveled for shipments with complex routes? Round the result to 2 decimal places. · _Moderate_

Expected → **`2633.48`**

<details><summary>gold SQL</summary>

```sql
WITH parsed AS (SELECT NULLIF(regexp_replace(t."GeoDistScore", '[^0-9\\.]', '', 'g'), '')::numeric AS geo_km FROM transactions t WHERE t."RouteComplex" = 'Complex') SELECT ROUND(AVG(geo_km), 2) AS avg_geo_dist_complex FROM parsed WHERE geo_km IS NOT NULL;
```
</details>

**16.** I want to know the average keyword-hitting values for all customer and internal chats to identify high-risk patterns. Round to 3 decimal places. · _Hard_

Expected → **`0.084`**

<details><summary>gold SQL</summary>

```sql
WITH ssd AS (SELECT (cm.communication_details->>'keyword_match_count')::numeric/NULLIF((cm.communication_details->>'msg_count_total')::numeric, 0) AS ssd FROM communications cm) SELECT ROUND(AVG(ssd), 3) AS avg_ssd FROM ssd;
```
</details>

**17.** Give me a list of sellers with their transaction flow scores, plus details about how complicated their shipping networks are. · _Hard_

Expected → **`V48710|999.05|1|1|0|0|100.00|0.00|0.00 ; V78592|998.52|1|1|0|0|100.00|0.00|0.00 ; V73782|998.17|1|1|0|0|100.00|0.00|0.00 ; V13892|997.62|1|0|0|1|0.00|0.00|100.0 …(+989 rows)`**

<details><summary>gold SQL</summary>

```sql
WITH v_plr AS (SELECT v."SellerKey",NULLIF(REGEXP_REPLACE(v."vendor_compliance_ratings"->>'liq_rate','[^0-9.]','','g'),'')::numeric AS plr FROM vendors v) SELECT p."SellerKey",p.plr,COUNT(t."EventCode") AS txn_count,COALESCE(SUM(CASE WHEN t."RouteComplex"='Simple' THEN 1 ELSE 0 END),0) AS simple_txn,COALESCE(SUM(CASE WHEN t."RouteComplex"='Medium' THEN 1 ELSE 0 END),0) AS medium_txn,COALESCE(SUM(CASE WHEN t."RouteComplex"='Complex' THEN 1 ELSE 0 END),0) AS complex_txn,ROUND(100.0*COALESCE(SUM(CASE WHEN t."RouteComplex"='Simple' THEN 1 ELSE 0 END),0)/NULLIF(COUNT(t."EventCode"),0),2) AS simple_pct,ROUND(100.0*COALESCE(SUM(CASE WHEN t."RouteComplex"='Medium' THEN 1 ELSE 0 END),0)/NULLIF(COUNT(t."EventCode"),0),2) AS medium_pct,ROUND(100.0*COALESCE(SUM(CASE WHEN t."RouteComplex"='Complex' THEN 1 ELSE 0 END),0)/NULLIF(COUNT(t."EventCode"),0),2) AS complex_pct FROM v_plr p LEFT JOIN transactions t ON t."VendorLink"=p."SellerKey" GROUP BY p."SellerKey",p.plr ORDER BY p.plr DESC NULLS LAST;
```
</details>

**18.** Show me all protected platforms, whether they're up or down, how many serious escalation cases they have, and how bad their current alerts are. · _Hard_

Expected → **`MK1020|Suspended|0|0|1|0|0 ; MK1070|Suspended|0|0|1|0|0 ; MK1077|Closed|2|1|1|0|0 ; MK1109|Closed|1|1|0|0|0 ; MK1130|Closed|0|0|1|0|0 …(+326 rows)`**

<details><summary>gold SQL</summary>

```sql
WITH secure_platforms AS (SELECT m."PlatCode", m."OperStatus" FROM markets m WHERE m.platform_compliance->>'sec_audit_stat' = 'Pass') SELECT s."PlatCode", s."OperStatus", COUNT(*) FILTER (WHERE a.alert_case_management->>'escalation_tier_stat' = 'Level3') AS tier3_case_count, COUNT(*) FILTER (WHERE a.alert_case_management->>'severity_level_stat' = 'Critical') AS critical_alerts, COUNT(*) FILTER (WHERE a.alert_case_management->>'severity_level_stat' = 'High') AS high_alerts, COUNT(*) FILTER (WHERE a.alert_case_management->>'severity_level_stat' = 'Medium') AS medium_alerts, COUNT(*) FILTER (WHERE a.alert_case_management->>'severity_level_stat' = 'Low') AS low_alerts FROM secure_platforms s LEFT JOIN transactions t ON t."PlatformKey" = s."PlatCode" LEFT JOIN alerts a ON a."EventTag" = t."EventCode" GROUP BY s."PlatCode", s."OperStatus" ORDER BY s."PlatCode";
```
</details>

**19.** Tell me how many live listings we have in each category, along with which ones have weird descriptions and how many sketchy buyers are interacting with them. · _Extra-Hard_

Expected → **`Physical|263|19957|21355|24527|49|263|18.63 ; Data|258|20283|22021|23113|32|256|12.50 ; Digital|242|19212|19889|21533|34|243|13.99 ; Service|236|20535|23737|160`**

<details><summary>gold SQL</summary>

```sql
WITH active_listings_by_cat AS (SELECT p."ProdCat" AS product_category, COUNT(*) AS active_listing_count FROM products p WHERE COALESCE((p.product_availability->>'qty_avail')::bigint, 0) > 0 GROUP BY p."ProdCat"), lang_metrics_by_cat AS (SELECT tp."ProdCat" AS product_category, COALESCE(SUM((c.communication_details->>'msg_count_total')::bigint) FILTER (WHERE c.communication_details->>'lang_pattern_type' = 'Suspicious'), 0) AS suspicious_msg_count, COALESCE(SUM((c.communication_details->>'msg_count_total')::bigint) FILTER (WHERE c.communication_details->>'lang_pattern_type' = 'Variable'), 0) AS variable_msg_count, COALESCE(SUM((c.communication_details->>'msg_count_total')::bigint) FILTER (WHERE c.communication_details->>'lang_pattern_type' = 'Consistent'), 0) AS consistent_msg_count FROM transaction_products tp JOIN communications c ON c."EventLink" = tp."EventLink" GROUP BY tp."ProdCat"), buyer_metrics AS (SELECT b."AcqCode", (b.buyer_risk_profile->>'behavior_consistency_scr')::numeric AS bcs, NULLIF(REGEXP_REPLACE(b.buyer_risk_profile->>'risk_dollar_ratio', '[^0-9.]', '', 'g'), '')::numeric AS brdr FROM buyers b), medians AS (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY bcs) AS median_bcs, percentile_cont(0.5) WITHIN GROUP (ORDER BY brdr) AS median_brdr FROM buyer_metrics WHERE bcs IS NOT NULL AND brdr IS NOT NULL), suspicious_buyers AS (SELECT bm."AcqCode" FROM buyer_metrics bm CROSS JOIN medians m WHERE bm.bcs IS NOT NULL AND bm.brdr IS NOT NULL AND bm.bcs < m.median_bcs AND bm.brdr > m.median_brdr), sus_buyer_by_cat AS (SELECT tp."ProdCat" AS product_category, COUNT(DISTINCT t."AcqLink") FILTER (WHERE sb."AcqCode" IS NOT NULL) AS suspicious_buyer_count, COUNT(DISTINCT t."AcqLink") AS total_buyer_count FROM transaction_products tp JOIN transactions t ON t."EventCode" = tp."EventLink" LEFT JOIN suspicious_buyers sb ON sb."AcqCode" = t."AcqLink" GROUP BY tp."ProdCat") SELECT a.product_category, a.active_listing_count, COALESCE(l.suspicious_msg_count, 0) AS suspicious_msg_count, COALESCE(l.variable_msg_count, 0) AS variable_msg_count, COALESCE(l.consistent_msg_count, 0) AS consistent_msg_count, COALESCE(s.suspicious_buyer_count, 0) AS suspicious_buyer_count, COALESCE(s.total_buyer_count, 0) AS total_buyer_count, CASE WHEN COALESCE(s.total_buyer_count, 0) = 0 THEN 0 ELSE ROUND((COALESCE(s.suspicious_buyer_count, 0)::numeric / s.total_buyer_count) * 100, 2) END AS suspicious_buyer_pct FROM active_listings_by_cat a LEFT JOIN lang_metrics_by_cat l USING (product_category) LEFT JOIN sus_buyer_by_cat s USING (product_category) ORDER BY a.active_listing_count DESC, a.product_category;
```
</details>

**20.** Split shoppers into three risk-per-dollar groups; for each group, show how many shoppers there are, what fraction of their orders go across countries, and how often their sessions look highly/medium/low hidden. · _Extra-Hard_

Expected → **`Low|330|330|48.48|0.00|0.00|0.00 ; Medium|331|337|50.45|0.00|0.00|0.00 ; High|329|333|57.06|0.00|0.00|0.00`**

<details><summary>gold SQL</summary>

```sql
WITH brdr AS (SELECT b."AcqCode" AS buyer_id,NULLIF(REGEXP_REPLACE(b."buyer_risk_profile"->>'risk_dollar_ratio','[^0-9.]','','g'),'')::numeric AS brdr FROM buyers b),th AS (SELECT percentile_disc(0.3333) WITHIN GROUP(ORDER BY brdr) AS p1,percentile_disc(0.6667) WITHIN GROUP(ORDER BY brdr) AS p2 FROM brdr WHERE brdr IS NOT NULL),bucketed_buyers AS (SELECT br.buyer_id,CASE WHEN br.brdr IS NULL THEN NULL WHEN br.brdr<=th.p1 THEN 'Low' WHEN br.brdr<=th.p2 THEN 'Medium' ELSE 'High' END AS risk_bucket FROM brdr br CROSS JOIN th),buyer_set AS (SELECT buyer_id,risk_bucket FROM bucketed_buyers WHERE risk_bucket IS NOT NULL),tx AS (SELECT bs.risk_bucket,t."EventCode" AS event_code,LOWER(COALESCE(t."CrossBorder"::text,'')) IN('true','t','1','yes','y') AS is_cross_border FROM buyer_set bs JOIN transactions t ON t."AcqLink"=bs.buyer_id),sess AS (SELECT tx.risk_bucket,tx.event_code,LOWER(cs."AnonLevel") AS anon_level FROM tx LEFT JOIN connection_security cs ON cs."TxnPointer"=tx.event_code),buyers_agg AS (SELECT risk_bucket,COUNT(DISTINCT buyer_id) AS buyer_count FROM buyer_set GROUP BY risk_bucket),tx_agg AS (SELECT risk_bucket,COUNT(DISTINCT event_code) AS txn_count,COUNT(DISTINCT CASE WHEN is_cross_border THEN event_code END) AS cross_border_txn_count FROM tx GROUP BY risk_bucket),sess_agg AS (SELECT risk_bucket,COUNT(DISTINCT event_code) AS sess_count,COUNT(DISTINCT CASE WHEN anon_level='high' THEN event_code END) AS anon_high_sessions,COUNT(DISTINCT CASE WHEN anon_level='medium' THEN event_code END) AS anon_medium_sessions,COUNT(DISTINCT CASE WHEN anon_level='low' THEN event_code END) AS anon_low_sessions FROM sess GROUP BY risk_bucket) SELECT b.risk_bucket,b.buyer_count,COALESCE(t.txn_count,0) AS txn_count,ROUND(100.0*COALESCE(t.cross_border_txn_count,0)/NULLIF(COALESCE(t.txn_count,0),0),2) AS cross_border_txn_pct,ROUND(100.0*COALESCE(s.anon_high_sessions,0)/NULLIF(COALESCE(s.sess_count,0),0),2) AS anon_high_pct,ROUND(100.0*COALESCE(s.anon_medium_sessions,0)/NULLIF(COALESCE(s.sess_count,0),0),2) AS anon_medium_pct,ROUND(100.0*COALESCE(s.anon_low_sessions,0)/NULLIF(COALESCE(s.sess_count,0),0),2) AS anon_low_pct FROM buyers_agg b LEFT JOIN tx_agg t ON t.risk_bucket=b.risk_bucket LEFT JOIN sess_agg s ON s.risk_bucket=b.risk_bucket ORDER BY CASE b.risk_bucket WHEN 'Low' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END;
```
</details>


---

## 2. sports_events_large — factual  ·  graph `sports_events_large`

_Direct counts / lookups (questions in Russian)._

**1.** Сколько всего Гран-при (гонок) в базе? · _Simple_

Expected → **`1125`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) FROM races;
```
</details>

**2.** Сколько всего гонщиков в базе? · _Simple_

Expected → **`861`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) FROM drivers;
```
</details>

**3.** Сколько автодромов (трасс) в базе? · _Simple_

Expected → **`77`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) FROM circuits;
```
</details>

**4.** Сколько Гран-при прошло на автодромах Италии? · _Simple_

Expected → **`107`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) FROM races r JOIN circuits c ON r.trkbind=c.cctkey WHERE c.location_metadata->'location'->>'country'='Italy';
```
</details>

**5.** Сколько Гран-при состоялось в сезоне 2023 года? · _Simple_

Expected → **`22`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) FROM races WHERE yr=2023;
```
</details>

**6.** Гонщиков какой национальности больше всего (национальность + число)? · _Medium_

Expected → **`British | 108`**

<details><summary>gold SQL</summary>

```sql
SELECT d.driver_identity->>'nationality' AS nationality, COUNT(*) AS n FROM drivers d WHERE d.driver_identity->>'nationality' IS NOT NULL GROUP BY nationality ORDER BY n DESC NULLS LAST LIMIT 1;
```
</details>

**7.** В каком сезоне прошло больше всего Гран-при (год + число)? · _Medium_

Expected → **`2024 | 24`**

<details><summary>gold SQL</summary>

```sql
SELECT yr, COUNT(*) AS races FROM races GROUP BY yr ORDER BY races DESC NULLS LAST LIMIT 1;
```
</details>

**8.** Сколько всего поул-позиций зафиксировано в квалификациях? · _Medium_

Expected → **`86`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(*) FROM qualifying WHERE is_pole_position = TRUE;
```
</details>

**9.** Самое быстрое время круга в секундах (исключая нулевые/отрицательные)? · _Medium_

Expected → **`57.481`**

<details><summary>gold SQL</summary>

```sql
SELECT MIN(msec_val/1000.0) AS fastest_lap_sec FROM lap_times WHERE msec_val>0;
```
</details>

**10.** Средняя длительность пит-стопа в секундах, округлить до 3 знаков? · _Medium_

Expected → **`83.399`**

<details><summary>gold SQL</summary>

```sql
SELECT ROUND(CAST(AVG(ms_count/1000.0) AS numeric),3) AS avg_pit_sec FROM pit_stops WHERE ms_count>0;
```
</details>

**11.** Гонщик с наибольшим числом поул-позиций (идентификатор + число)? · _Hard_

Expected → **`hamilton | 18`**

<details><summary>gold SQL</summary>

```sql
SELECT d.driver_identity->>'reference' AS driver, COUNT(*) AS poles FROM qualifying q JOIN drivers d ON q.pilotrec=d.drv_main WHERE q.is_pole_position GROUP BY driver ORDER BY poles DESC NULLS LAST LIMIT 1;
```
</details>

**12.** Конструктор с наибольшим числом поул-позиций (название + число)? · _Hard_

Expected → **`Mercedes | 21`**

<details><summary>gold SQL</summary>

```sql
SELECT con.namelabel AS constructor, COUNT(*) AS poles FROM qualifying q JOIN constructors con ON q.corptag=con.cstr_key WHERE q.is_pole_position GROUP BY con.namelabel ORDER BY poles DESC NULLS LAST LIMIT 1;
```
</details>

**13.** Три страны с наибольшим числом проведённых Гран-при (страна + число)? · _Hard_

Expected → **`Italy|107 ; Germany|79 ; UK|79`**

<details><summary>gold SQL</summary>

```sql
SELECT c.location_metadata->'location'->>'country' AS country, COUNT(*) AS races FROM races r JOIN circuits c ON r.trkbind=c.cctkey GROUP BY country ORDER BY races DESC NULLS LAST LIMIT 3;
```
</details>

**14.** Сколько различных стран принимали Гран-при? · _Hard_

Expected → **`35`**

<details><summary>gold SQL</summary>

```sql
SELECT COUNT(DISTINCT c.location_metadata->'location'->>'country') FROM races r JOIN circuits c ON r.trkbind=c.cctkey;
```
</details>


---

## 3. sports_events_large — analytical  ·  graph `sports_events_large`

_Multi-step / metric / window questions (Simple → Extra-Hard)._

**1.** What's the absolute fastest lap time ever recorded in our database, measured in seconds? Make sure to ignore any zero or negative times. · _Simple_

Expected → **`57.4810000000000000`**

<details><summary>gold SQL</summary>

```sql
-- Intent: Identify the fastest single lap time recorded in the database
-- Step 1: Filter out invalid lap times (zero or negative values)
-- Step 2: Find minimum lap time using MIN function
SELECT MIN(msec_val / 1000.0) AS fastest_lap_seconds
FROM lap_times
WHERE msec_val > 0;
```
</details>

**2.** To do the performance analysis, please help me calculate the average duration of our pit stops (in seconds), excluding any records where the duration is not a positive value. I want a single output, rounded to three decimal places. · _Simple_

Expected → **`83.399`**

<details><summary>gold SQL</summary>

```sql
-- Intent: Calculate the average time cars spend in pit stops
-- Step 1: Convert milliseconds to seconds using division
-- Step 2: Calculate average using AVG function with NULL filtering
SELECT ROUND(AVG(ms_count / 1000.0), 3) AS average_pit_stop_seconds
FROM pit_stops
WHERE ms_count > 0 AND ms_count IS NOT NULL;
```
</details>

**3.** Our team is studying how thin air affects car performance at racing venues. Can you pull up a list of all tracks that are located high enough above sea level to create high-altitude circuit? I need to see the track names and their exact elevations, with the highest altitude venues listed first. · _Simple_

Expected → **`Autódromo Hermanos Rodríguez|2227 ; Kyalami|1460`**

<details><summary>gold SQL</summary>

```sql
-- Intent: To retrieve a list of all circuits that are considered high-altitude.
-- Knowledge Used: "High-Altitude Circuit" (ID: 14)
-- Advanced Functions: Basic SELECT with WHERE clause, JSONB operators
SELECT
    -- Step 1: Select the name and elevation of the circuit from the 'location_metadata' JSON.
    -- Step 2: Filter for circuits where the elevation is greater than 800 meters.
    location_metadata->>'name' AS circuit_name,
    (location_metadata->'coordinates'->>'elevation_m')::integer AS elevation
FROM circuits
WHERE (location_metadata->'coordinates'->>'elevation_m')::integer > 800
ORDER BY elevation DESC;
```
</details>

**4.** I need to know if we have any circuits with specific environmental characteristics regarded as 'high-altitude'. Can you just give me a simple 'Yes' or 'No' answer? · _Moderate_

Expected → **`Yes`**

<details><summary>gold SQL</summary>

```sql
-- Intent: Determine if the database contains any high-altitude circuits (>800m elevation)
-- Step 1: Extract elevation data from JSON metadata
-- Step 2: Check if any circuit exceeds 800m threshold using conditional logic
SELECT CASE WHEN EXISTS(SELECT 1 FROM circuits WHERE (location_metadata->'coordinates'->>'elevation_m')::NUMERIC > 800) 
        THEN 'Yes' 
        ELSE 'No' 
    END AS has_high_altitude_circuits;
```
</details>

**5.** Please show me the average age of all sprint session winners at the time they won. The result should be a single age in years. · _Moderate_

Expected → **`26`**

<details><summary>gold SQL</summary>

```sql
-- Intent: To calculate the average age of all drivers at the time they won a race.
-- Advanced Functions: Aggregate Function (AVG), Date/Time Function (AGE), JSONB operators
SELECT
    EXTRACT(YEAR FROM AVG(AGE((r.event_schedule->>'date_set')::date, (d.driver_identity->>'birth_date')::date))) AS average_winner_age
FROM sprint_results sr
JOIN drivers d ON sr.unitDrive = d.DRV_MAIN
JOIN races r ON sr.matchRef = r.RAK_ID
WHERE (sr.sprint_performance->>'ranking_order')::integer = 1;
```
</details>

**6.** I need to generate a list that ranks the drivers' overall performance in a Sprint session. The output should include the event name, the driver's ID, and their performance index score. Please make sure the best performances are right at the top. · _Moderate_

Expected → **`|norris|16 ; |max_verstappen|16 ; Miami Grand Prix|max_verstappen|16 ; Azerbaijan Grand Prix|perez|16 ; Qatar Grand Prix|piastri|16 …(+340 rows)`**

<details><summary>gold SQL</summary>

```sql
SELECT
    r.event_schedule->>'event_name' as event,
    d.driver_identity->>'reference' AS driver,
    (9 - (sr.sprint_performance->>'final_position')::integer) + (sr.sprint_performance->>'points')::integer AS sprint_performance_index
FROM sprint_results sr
JOIN races r ON sr.matchref = r.rak_id
JOIN drivers d ON sr.unitdrive = d.drv_main
WHERE sr.sprint_performance->>'final_position' IS NOT NULL -- Ensure the driver finished
ORDER BY sprint_performance_index DESC;
```
</details>

**7.** Can you calculate how well the top 8 finishers perform on average in sprint sessions? Round the result to two decimal places. · _Hard_

Expected → **`8.38`**

<details><summary>gold SQL</summary>

```sql
-- Intent: Calculate average sprint performance index across all sprint participants
-- Step 1: Extract sprint positions and points from JSON performance data
WITH sprint_analysis AS (
    SELECT 
        unitdrive,
        (sprint_performance->>'final_position')::INTEGER as finish_position,
        (sprint_performance->>'points')::INTEGER as points_earned,
        -- Step 2: Apply Sprint Performance Index formula from knowledge base
        CASE 
            WHEN (sprint_performance->>'final_position')::INTEGER IS NOT NULL 
            THEN (9 - (sprint_performance->>'final_position')::INTEGER) + 
                 COALESCE((sprint_performance->>'points')::INTEGER, 0)
            ELSE NULL
        END AS sprint_performance_index
    FROM sprint_results
    WHERE sprint_performance->>'final_position' IS NOT NULL
      AND (sprint_performance->>'final_position')::INTEGER <= 8
)
-- Step 3: Calculate average SPI using statistical aggregation
SELECT ROUND(
    AVG(sprint_performance_index), 2
) AS average_sprint_performance_index
FROM sprint_analysis
WHERE sprint_performance_index IS NOT NULL;
```
</details>

**8.** Which constructor has the best track record for finishing races? Calculate the reliability rate among all constructors who have participated in at least 5 races, which shows the races finished out of races started, and return the highest reliability percentage, rounded to two decimal places. · _Hard_

Expected → **`100.00`**

<details><summary>gold SQL</summary>

```sql
-- Intent: Find the highest constructor reliability rate in the dataset
-- Step 1: Count total starts and finishes per constructor using advanced grouping
WITH constructor_reliability AS (
    SELECT 
        cr.unitnode,
        COUNT(*) as total_starts,
        -- Step 2: Count finishes (assuming non-null scoreval indicates finish)
        COUNT(*) FILTER (WHERE cr.scoreval IS NOT NULL) as total_finishes,
        -- Step 3: Calculate reliability rate using the knowledge base formula
        CASE 
            WHEN COUNT(*) > 0 
            THEN (COUNT(*) FILTER (WHERE cr.scoreval IS NOT NULL))::DECIMAL / COUNT(*) * 100
            ELSE 0
        END as reliability_rate
    FROM constructor_results cr
    GROUP BY cr.unitnode
    HAVING COUNT(*) >= 5  -- Minimum sample size for statistical significance
)
-- Step 4: Find maximum reliability rate using advanced ranking
SELECT ROUND(MAX(reliability_rate), 2) AS highest_constructor_reliability_rate
FROM constructor_reliability;
```
</details>

**9.** Can you calculate the average stops per car for each event, and just show me the total count of races classified as a 'Single-Stop Race' based on the pit strategy classification criteria? · _Hard_

Expected → **`269`**

<details><summary>gold SQL</summary>

```sql
WITH stop_counts AS (
    SELECT 
        matchidx,
        COUNT(*) * 1.0 / COUNT(DISTINCT wunit) AS avg_stops_per_car
    FROM pit_stops
    GROUP BY matchidx
),
strategy_classification AS (
    SELECT 
        matchidx,
        CASE 
            WHEN avg_stops_per_car < 1.5 THEN 'Single-Stop Race'
            WHEN avg_stops_per_car < 2.5 THEN 'Standard Two-Stop'
            ELSE 'High-Strategy Event'
        END AS pit_strategy_cluster
    FROM stop_counts
)
SELECT 
    COUNT(*) AS single_stop_race_count
FROM strategy_classification
WHERE LOWER(TRIM(pit_strategy_cluster)) = 'single-stop race';
```
</details>

**10.** Can you rank the drivers based on the stability of their average lap time? Please show me each driver's surname and first_name in a JSON format, their average consistency score, and the number of Races Analyzed. Just focus on drivers who have competed in more than five races, and list the most consistent ones at the top. · _Hard_

Expected → **`{"surname": "Ericsson", "first_name": "Marcus"}|0.14990663761154807517297900476623|9 ; {"surname": "Kubica", "first_name": "Robert"}|0.3330472939388638839927976 …(+63 rows)`**

<details><summary>gold SQL</summary>

```sql
/* Intent: Measure driver consistency using Lap Time Consistency knowledge */
WITH driver_laps AS (
    /* Step 1: Get all lap times in seconds */
    SELECT wheel_unit AS driver_id,
           msec_val/1000.0 AS lap_time_sec,
           rc_index AS race_id
    FROM lap_times
),
stats AS (
    /* Step 2: Calculate mean and standard deviation per driver */
    SELECT driver_id,
           race_id,
           AVG(lap_time_sec) AS avg_lap,
           STDDEV(lap_time_sec) AS lap_stddev
    FROM driver_laps
    GROUP BY driver_id, race_id
)
/* Step 3: Rank drivers by consistency */
SELECT d.driver_identity->>'name' AS driver_name,
       AVG(s.lap_stddev) AS avg_consistency,
       COUNT(DISTINCT s.race_id) AS races_analyzed
FROM stats s
JOIN drivers d ON s.driver_id = d.drv_main
GROUP BY d.drv_main, d.driver_identity->>'name'
HAVING COUNT(DISTINCT s.race_id) > 5
ORDER BY avg_consistency;
```
</details>

**11.** I'm interested in the achievements of veterans. Could you pull up a list of the race year, official race event name, driver's full name, their podium position, and their age at the time of the race. Please show accomplishments by oldest drivers first, and for same-age drivers, show most recent results first. · _Hard_

Expected → **`1950|Belgian Grand Prix|Luigi Fagioli|2|52 ; 1957|||1|46 ; 1955|||1|44 ; 1950|Belgian Grand Prix|Nino Farina|1|44 ; 1951|Belgian Grand Prix||2|40 …(+20 rows)`**

<details><summary>gold SQL</summary>

```sql
-- Step 1: Directly query the driver standings, filtering for podium finishes (positions 1, 2, or 3).
-- Step 2: Join with the races table to get the year of the race and the drivers table to get their birth date.
-- Step 3: In the WHERE clause, calculate the driver's age at the time of the race by subtracting their birth year from the race year.
-- Step 4: Filter for instances where this calculated age is 35 or greater.
SELECT
    r.yr AS race_year,
    r.event_schedule->>'event_name' AS race_event,
    (d.driver_identity->'name'->>'first_name') || ' ' || (d.driver_identity->'name'->>'surname') AS driver_name,
    ds.px AS podium_position,
    (r.yr - EXTRACT(YEAR FROM (d.driver_identity->>'birth_date')::date)) AS age_at_race
FROM
    driver_standings ds
JOIN
    drivers d ON ds.drive_link = d.drv_main
JOIN
    races r ON ds.rlink = r.rak_id
WHERE
    ds.px IN (1, 2, 3)
AND
    (r.yr - EXTRACT(YEAR FROM (d.driver_identity->>'birth_date')::date)) >= 35
ORDER BY
    age_at_race DESC, race_year DESC;
```
</details>

**12.** To analyze team performance, can you calculate the rate of constructor reliability for each team? I need to see the team names, their total races started, total races finished, the reliability rate as a percentage, and give them a reliability rank. Only include constructors with significant participation so we get meaningful data, and sort them from most reliable to least reliable. · _Extra-Hard_

Expected → **`Honda|14|14|100|1 ; Osella|16|16|100|1 ; |47|47|100|1 ; RAM|12|12|100|1 ; Lola|11|11|100|1 …(+39 rows)`**

<details><summary>gold SQL</summary>

```sql
WITH ConstructorRaceStats AS (SELECT cr.unitNode, c.NameLabel, COUNT(cr.matchRef) AS total_races_started, SUM(CASE WHEN cr.ST_mark is NULL THEN 1 ELSE 0 END) AS total_races_finished FROM constructor_results AS cr JOIN constructors AS c ON cr.unitNode = c.CSTR_Key GROUP BY cr.unitNode, c.NameLabel), ConstructorReliability AS (SELECT NameLabel, total_races_started, total_races_finished, (CAST(total_races_finished AS REAL) * 100 / total_races_started) AS reliability_rate FROM ConstructorRaceStats WHERE total_races_started > 10) SELECT NameLabel AS constructor_name, total_races_started, total_races_finished, reliability_rate, RANK() OVER (ORDER BY reliability_rate DESC) AS reliability_rank FROM ConstructorReliability ORDER BY reliability_rate DESC;
```
</details>

**13.** I'm curious about how Hamilton's value as a driver changes as he gets older and more experienced. Please give me the race IDs and his performance values. · _Extra-Hard_

Expected → **`28| ; 31|2.6956521739130435 ; 342|1.56 ; 350|0.7866666666666667 ; 352| …(+14 rows)`**

<details><summary>gold SQL</summary>

```sql
-- Intent: To track the performance value of a specific driver over a season.
-- Knowledge Used: "Driver Age" (ID: 20), "Driver's Points Per Race (PPR)" (ID: 26), "Driver Performance Value" (ID: 37)
-- Advanced Functions: CTE, Window Functions, Date/Time functions, JSONB operators
WITH DriverData AS (
    -- Step 1: Get driver's age and PPR before each race, extracting dates and names from JSON.
    SELECT
        ds.rlink AS race_id,
        ds.Drive_Link AS driver_id,
        EXTRACT(YEAR FROM AGE((r.event_schedule->>'date_set')::date, (d.driver_identity->>'birth_date')::date)) as driver_age,
        (LAG(ds.acc_pt, 1, 0) OVER (PARTITION BY ds.Drive_Link ORDER BY ds.rlink)) /
            NULLIF((ROW_NUMBER() OVER (PARTITION BY ds.Drive_Link ORDER BY ds.rlink)) - 1, 0) as ppr
    FROM driver_standings ds
    JOIN drivers d ON ds.Drive_Link = d.DRV_MAIN
    JOIN races r ON ds.rlink = r.RAK_ID
    WHERE LOWER(TRIM(d.driver_identity->>'reference')) = 'hamilton' 
)
-- Step 2: Calculate DPV for each race.
SELECT
    race_id,
    ppr / NULLIF(driver_age, 0) AS dpv
FROM DriverData
WHERE driver_age > 0;
```
</details>

**14.** I want to see how McLaren's overall team performance develops over seasons - like watching their report card get updated after each race. Just show me the year, race ID, constructor name, and the cumulative constructor's performance score after each race event, so I can track how their performance score changes as the season progresses. · _Extra-Hard_

Expected → **`1972|621|McLaren|9 ; 1972|627|McLaren|11 ; 1972|630|McLaren|21 ; 1972|631|McLaren|25 ; 1973|605|McLaren|2 …(+145 rows)`**

<details><summary>gold SQL</summary>

```sql
WITH McLarenRaceData AS (SELECT r.Yr, r.RAK_ID, r.rNUM, c.NameLabel, cr.scoreVal, cr.ST_mark FROM races AS r JOIN constructor_results AS cr ON r.RAK_ID = cr.matchRef JOIN constructors AS c ON cr.unitNode = c.CSTR_Key WHERE LOWER(TRIM(c.NameLabel)) = 'mclaren'), CumulativeStats AS (SELECT Yr, RAK_ID, rNUM, NameLabel, SUM(COALESCE(scoreVal, 0)) OVER (PARTITION BY Yr ORDER BY rNUM) AS cumulative_season_points, COUNT(*) OVER (PARTITION BY Yr ORDER BY rNUM) AS cumulative_starts, SUM(CASE WHEN ST_mark IS NULL THEN 1 ELSE 0 END) OVER (PARTITION BY Yr ORDER BY rNUM) AS cumulative_finishes FROM McLarenRaceData) SELECT Yr AS year, RAK_ID AS race_id, NameLabel AS constructor_name, (cumulative_season_points * (cumulative_finishes * 1.0 / cumulative_starts)) AS cumulative_cps FROM CumulativeStats ORDER BY year, rNUM;
```
</details>

