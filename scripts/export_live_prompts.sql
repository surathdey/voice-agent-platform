-- Read-only export of the five live agents' CURRENT prompts + structured
-- config, for the Epic C catalog v1 seed (C2 step 6). Run against the
-- LIVE database via:  docker compose exec -T db psql -U <user> -d <db>
--   < scripts/export_live_prompts.sql
-- This SELECT writes nothing. The agent.prompt column is the live prompt
-- (PUT keeps it equal to the latest PromptVersion row); prompt_version
-- below is that latest version number, so the seed can verify the two
-- agree (RPI v10, Reminder v2, PTP v7, Settlement v8, Broken v2).
SELECT json_agg(row_to_json(t)) FROM (
  SELECT a.agent_id, a.name, a.stage, a.purpose, a.prompt,
    (SELECT version FROM prompt_versions pv
      WHERE pv.agent_id = a.agent_id ORDER BY version DESC LIMIT 1) AS prompt_version,
    (SELECT prompt FROM prompt_versions pv
      WHERE pv.agent_id = a.agent_id ORDER BY version DESC LIMIT 1) AS latest_version_prompt,
    a.llm_model, a.stt_provider, a.stt_language, a.tts_provider,
    a.tts_voice_id, a.voice_gender, a.language_rule, a.captured_fields,
    a.max_duration_s, a.max_turns, a.silence_prompt_after_s,
    a.silence_max_prompts
  FROM agent_configs a
  WHERE a.agent_id IN (
    'agt_4f14a6e666be443e',
    'agt_31e583f023504b21',
    'agt_cdf5481a70544bb9',
    'agt_2c5c61b24daa4dc0',
    'agt_d737173f9e8f4840'
  )
  ORDER BY a.name
) t;
