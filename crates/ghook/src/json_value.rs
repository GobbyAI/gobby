use serde_json::Value;

pub(crate) fn is_python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => {
            if let Some(i) = number.as_i64() {
                i != 0
            } else if let Some(u) = number.as_u64() {
                u != 0
            } else {
                number.as_f64().is_some_and(|f| f != 0.0)
            }
        }
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_truthiness_matches_dispatcher_semantics() {
        for value in [
            json!(null),
            json!(false),
            json!(0),
            json!(0.0),
            json!(""),
            json!([]),
            json!({}),
        ] {
            assert!(!is_python_truthy(&value), "{value:?} should be falsy");
        }

        for value in [
            json!(true),
            json!(1),
            json!(-1),
            json!(0.25),
            json!("text"),
            json!([null]),
            json!({"key": null}),
        ] {
            assert!(is_python_truthy(&value), "{value:?} should be truthy");
        }
    }
}
