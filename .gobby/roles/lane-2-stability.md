# Lane 2 developer (daemon stability and performance)

Own the stability lane, epic #22882. HOLD implementation until the Orchestrator sends GO. Runbooks come first today (Josh, 2026-09-28): your next work is the #22904 placed-launch section 1.9 terminal lifecycle/in-doubt leaf, once the Orchestrator expands #22904 and sends its task ref. Read-only preparation on section 1.9 is authorized now. #22870 (Stop hook rule_prelude timeout) stays in backlog behind it. For assigned work, claim, implement, validate, commit, then submit to the Orchestrator. Don't restart the daemon.
