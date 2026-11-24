-- Example queries. Run one with:
--   sqlite3 -header -column scout.db "<query>"
-- or open scout.db in DB Browser for SQLite and paste a query in "Execute SQL".
--
-- Views (recreated every time scout opens the database):
--   v_events       canonical events, human edits applied, excluded rows hidden
--   v_events_all   same, including rows marked exclude
--   v_companies    one row per company with counts by event type
--   v_monthly      events per month x event_type x origin (news / registry)
-- Tables: events (raw), edits (corrections from the Google Sheet), seen_items,
--         runs, llm_calls.

-- 1. Latest 20 events
SELECT event_date, event_type, detail, drug, sponsor, n_reports, first_url
FROM v_events ORDER BY event_date DESC LIMIT 20;

-- 2. Trial failures from news (not registry), with their headline
SELECT event_date, detail, drug, sponsor, headline
FROM v_events WHERE event_type = 'trial_failure' AND origin = 'news'
ORDER BY event_date DESC;

-- 3. All CRLs and their reason
SELECT event_date, detail AS crl_reason, drug, sponsor, headline
FROM v_events WHERE event_type = 'regulatory_rejection' ORDER BY event_date DESC;

-- 4. Companies with the most events
SELECT company, n_events, n_trial_failure, n_clinical_hold, n_regulatory_rejection,
       n_business_discontinuation, latest_event
FROM v_companies ORDER BY n_events DESC LIMIT 20;

-- 5. Monthly counts by event type
SELECT month, event_type, SUM(n_events) AS n FROM v_monthly
GROUP BY month, event_type ORDER BY month DESC, n DESC;

-- 6. Events worth a manual check: quote or fields unverified, or registry conflict
SELECT event_key, event_type, drug, sponsor, quote_verified, unverified_fields, registry_conflict
FROM v_events
WHERE quote_verified = 0 OR unverified_fields != '[]' OR registry_conflict = 1;

-- 7. Events reported by several outlets (merged duplicates)
SELECT event_date, event_type, drug, sponsor, n_reports FROM v_events
WHERE n_reports > 1 ORDER BY n_reports DESC;

-- 8. Human corrections so far
SELECT v.event_key, v.model_event_type, v.corrected_type, v.exclude, v.notes
FROM v_events_all v WHERE v.corrected_type IS NOT NULL OR v.exclude = 1 OR v.notes IS NOT NULL;

-- 9. API cost per run
SELECT r.id, r.since, r.until, COUNT(c.id) AS calls, ROUND(SUM(c.cost_usd), 4) AS usd
FROM runs r LEFT JOIN llm_calls c ON c.run_id = r.id GROUP BY r.id ORDER BY r.id DESC;
