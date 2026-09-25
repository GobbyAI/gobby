# Monitor: gobby#14307

Run the gobby:log-monitor-tick checks (every 10 minutes). Report to the PD: "Systems nominal" when healthy, or EVENT=ALARM with evidence on a breach. Copy every ALARM to the Assistant (gobby#14069). Give performance verdicts (for example after a restart) only from matched time windows.
