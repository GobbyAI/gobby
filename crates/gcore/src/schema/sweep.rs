use postgres::Client;

fn test_schema_created_epoch(schema_name: &str) -> Option<i64> {
    // A non-public hub owns `<schema>_agent_auth`; it ages with its hub.
    let hub_schema = schema_name
        .strip_suffix("_agent_auth")
        .unwrap_or(schema_name);
    let mut parts = hub_schema.split('_');
    if parts.next()? != "gobby" || parts.next()? != "test" {
        return None;
    }
    let created_epoch = parts.next()?;
    let process_id = parts.next()?;
    let worker_label = parts.next()?;
    let nonce = parts.next()?;
    if parts.next().is_some()
        || created_epoch.is_empty()
        || !created_epoch.bytes().all(|byte| byte.is_ascii_digit())
        || process_id.is_empty()
        || !process_id.bytes().all(|byte| byte.is_ascii_digit())
        || worker_label.is_empty()
        || nonce.is_empty()
    {
        return None;
    }
    created_epoch.parse().ok()
}

fn test_schema_is_sweep_eligible(schema_name: &str, cutoff_epoch: i64) -> bool {
    test_schema_created_epoch(schema_name).is_some_and(|epoch| epoch <= cutoff_epoch)
}

pub fn sweep_test_schemas(
    client: &mut Client,
    cutoff_epoch: i64,
) -> Result<usize, postgres::Error> {
    let candidates = client.query(
        "SELECT schema_name FROM information_schema.schemata \
         WHERE schema_name LIKE 'gobby_test_%'",
        &[],
    )?;
    let mut dropped = 0;
    for candidate in candidates {
        let schema_name: String = candidate.get(0);
        let lease_acquired: bool = client
            .query_one("SELECT pg_try_advisory_lock(hashtext($1))", &[&schema_name])?
            .get(0);
        if !lease_acquired {
            continue;
        }

        let sweep_result = sweep_candidate(client, &schema_name, cutoff_epoch);
        let unlock_result =
            client.query_one("SELECT pg_advisory_unlock(hashtext($1))", &[&schema_name]);
        match sweep_result {
            Ok(was_dropped) => {
                unlock_result?;
                dropped += usize::from(was_dropped);
            }
            Err(error) => {
                let _ = unlock_result;
                return Err(error);
            }
        }
    }
    Ok(dropped)
}

fn sweep_candidate(
    client: &mut Client,
    candidate_name: &str,
    cutoff_epoch: i64,
) -> Result<bool, postgres::Error> {
    let current = client.query_opt(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = $1",
        &[&candidate_name],
    )?;
    let Some(current) = current else {
        return Ok(false);
    };
    let schema_name: String = current.get(0);
    if !test_schema_is_sweep_eligible(&schema_name, cutoff_epoch) {
        return Ok(false);
    }
    let quoted_name = schema_name.replace('"', "\"\"");
    client.batch_execute(&format!("DROP SCHEMA \"{quoted_name}\" CASCADE"))?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::test_schema_is_sweep_eligible;

    #[test]
    fn sweep_eligibility_requires_an_aged_six_part_test_schema_name() {
        assert!(test_schema_is_sweep_eligible(
            "gobby_test_100_42_master_abc123",
            101,
        ));
        assert!(!test_schema_is_sweep_eligible(
            "gobby_test_101_42_master_abc123",
            100,
        ));
        assert!(!test_schema_is_sweep_eligible(
            "gobby_test_100_42_master",
            101,
        ));
        assert!(!test_schema_is_sweep_eligible(
            "gobby_test_invalid_42_master_abc123",
            101,
        ));
        assert!(test_schema_is_sweep_eligible(
            "gobby_test_100_42_master_abc123_agent_auth",
            101,
        ));
        assert!(!test_schema_is_sweep_eligible(
            "gobby_test_101_42_master_abc123_agent_auth",
            100,
        ));
    }
}
